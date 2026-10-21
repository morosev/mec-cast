/*
 * mec-cast-e2-bridge: the mec-cast xApp's E2 work on FlexRIC.
 *
 * FlexRIC's Python SDK has no E2SM-KPM or E2SM-RC (only FlexRIC's own MAC,
 * RLC, PDCP, GTP and slicing models, which only the OAI gNB implements), so
 * the KPM subscription and the RC control are done here, in C, against
 * FlexRIC's xApp API (libe42_xapp). The Python xApp runs this program as a
 * child (ran/xapp/src/mec_cast_xapp/adapters/flexric.py) and talks to it in
 * the JSON E2 feed protocol, one object per line:
 *
 *   stdin  {"op":"nodes"}
 *          {"op":"subscribe","id","node","style","metrics":[..],"ue_ids":[..],"period_ms","sst"}
 *          {"op":"unsubscribe","id"}
 *          {"op":"control","id","node","action":"prb_quota","ue_id","min_prb_ratio",
 *           "max_prb_ratio","dedicated_prb_ratio","plmn","sst","sd"}
 *   stdout {"op":"nodes","nodes":[..]}
 *          {"op":"subscribed","id"} | {"op":"subscribe_failed","id","detail"}
 *          {"op":"indication","id","indication":{"e2_node_id","collect_start_ns","meas":{..}}}
 *          {"op":"control_ack","id","ok","detail"}
 *
 * End of stdin removes every subscription and exits.
 *
 * Three things in FlexRIC v3.0.0 shape this program, all in src/xApp:
 *  - control_sm_xapp_api() returns success whether the E2 node answered
 *    RIC Control Acknowledge or Failure. The only trace of which is a printf
 *    ("CONTROL ACK rx" / "CONTROL FAILURE rx"). So FlexRIC's stdout is a
 *    pipe this program reads, and the JSON protocol runs on a duplicate of
 *    the original stdout instead.
 *  - A RIC Subscription Failure aborts the process (its handler is an
 *    unimplemented assert). So a subscription is checked first against the
 *    report styles and measurement names the E2 node advertised in its RAN
 *    function definition, which catches the usual refusal, an unsupported
 *    metric, without asking. The adapter treats an abort right after a
 *    subscribe as a refusal too.
 *  - Report callbacks carry no user pointer, so each subscription gets one of
 *    a fixed set of slot callbacks.
 *
 * KPM report styles: 1 (node level, indication format 1) and 4 (every UE
 * matching an S-NSSAI, format 3, per UE). Style 2 and 5 are not built here.
 * The PRB quota is E2SM-RC Control Style 2, Action 6, with the RAN parameter
 * tree srsRAN/OCUDU parses (ids 1..13, no 4); the slice is a parameter, not
 * a constant.
 *
 * Links against FlexRIC (OAI CSSL v1.0) and contains none of its code.
 */

#include "src/xApp/e42_xapp_api.h"
#include "src/sm/rc_sm/ie/ir/ran_param_list.h"
#include "src/sm/rc_sm/ie/ir/ran_param_struct.h"
#include "src/util/byte_array.h"
#include "src/util/conf_file.h"

#include <ctype.h>
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define KPM_RAN_FUNC_ID 2
#define RC_RAN_FUNC_ID 3
#define MAX_SLOTS 16
#define MAX_LINE 65536

/* --- output: the JSON protocol on its own fd ------------------------------- */

static FILE* out;
static pthread_mutex_t out_mtx = PTHREAD_MUTEX_INITIALIZER;

typedef struct {
  char* buf;
  size_t len, cap;
} sbuf_t;

static void sb_grow(sbuf_t* b, size_t need)
{
  if (b->len + need + 1 <= b->cap)
    return;
  size_t cap = b->cap ? b->cap : 256;
  while (cap < b->len + need + 1)
    cap *= 2;
  b->buf = realloc(b->buf, cap);
  if (b->buf == NULL)
    abort();
  b->cap = cap;
}

static void sb_printf(sbuf_t* b, char const* fmt, ...)
{
  va_list ap;
  va_start(ap, fmt);
  va_list ap2;
  va_copy(ap2, ap);
  int n = vsnprintf(NULL, 0, fmt, ap);
  va_end(ap);
  sb_grow(b, (size_t)n);
  vsnprintf(b->buf + b->len, (size_t)n + 1, fmt, ap2);
  va_end(ap2);
  b->len += (size_t)n;
}

static void sb_str(sbuf_t* b, char const* s)
{
  sb_printf(b, "\"");
  for (; s && *s; s++) {
    unsigned char c = (unsigned char)*s;
    if (c == '"' || c == '\\')
      sb_printf(b, "\\%c", c);
    else if (c < 0x20)
      sb_printf(b, "\\u%04x", c);
    else
      sb_printf(b, "%c", c);
  }
  sb_printf(b, "\"");
}

static void emit(sbuf_t* b)
{
  pthread_mutex_lock(&out_mtx);
  fwrite(b->buf, 1, b->len, out);
  fputc('\n', out);
  fflush(out);
  pthread_mutex_unlock(&out_mtx);
  free(b->buf);
  *b = (sbuf_t){0};
}

static void emit_simple(char const* op, char const* id, char const* key, char const* val)
{
  sbuf_t b = {0};
  sb_printf(&b, "{\"op\":");
  sb_str(&b, op);
  if (id) {
    sb_printf(&b, ",\"id\":");
    sb_str(&b, id);
  }
  if (key) {
    sb_printf(&b, ",");
    sb_str(&b, key);
    sb_printf(&b, ":");
    sb_str(&b, val ? val : "");
  }
  sb_printf(&b, "}");
  emit(&b);
}

/* --- FlexRIC's stdout: forwarded to stderr, scanned for control answers ---- */

static pthread_mutex_t ctl_mtx = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t ctl_cv = PTHREAD_COND_INITIALIZER;
static int ctl_answer; /* 0 none yet, 1 ack, -1 failure */

static void* scan_flexric_stdout(void* arg)
{
  FILE* in = fdopen((int)(intptr_t)arg, "r");
  char line[4096];
  while (in && fgets(line, sizeof line, in)) {
    fputs(line, stderr);
    int answer = strstr(line, "CONTROL ACK rx") ? 1 : strstr(line, "CONTROL FAILURE rx") ? -1 : 0;
    if (answer) {
      pthread_mutex_lock(&ctl_mtx);
      ctl_answer = answer;
      pthread_cond_signal(&ctl_cv);
      pthread_mutex_unlock(&ctl_mtx);
    }
  }
  return NULL;
}

/* Keep the protocol fd, then point fd 1 (FlexRIC's printf) at the scanner. */
static void split_stdout(void)
{
  int proto = dup(STDOUT_FILENO);
  int p[2];
  if (proto < 0 || pipe(p) != 0 || dup2(p[1], STDOUT_FILENO) < 0) {
    perror("mec-cast-e2-bridge: cannot split stdout");
    exit(1);
  }
  close(p[1]);
  setvbuf(stdout, NULL, _IOLBF, 0); /* FlexRIC's prints reach the scanner per line */
  out = fdopen(proto, "w");
  pthread_t t;
  pthread_create(&t, NULL, scan_flexric_stdout, (void*)(intptr_t)p[0]);
  pthread_detach(t);
}

/* --- a minimal reader for the flat request objects ------------------------- */

/* Pointer to the value of "key" in a flat JSON object, or NULL. */
static char const* jfind(char const* s, char const* key)
{
  char pat[128];
  snprintf(pat, sizeof pat, "\"%s\"", key);
  for (char const* p = strstr(s, pat); p; p = strstr(p + 1, pat)) {
    char const* q = p + strlen(pat);
    while (isspace((unsigned char)*q))
      q++;
    if (*q == ':') {
      q++;
      while (isspace((unsigned char)*q))
        q++;
      return q;
    }
  }
  return NULL;
}

static bool jstr(char const* s, char const* key, char* dst, size_t n)
{
  char const* v = jfind(s, key);
  if (!v || *v != '"')
    return false;
  size_t i = 0;
  for (v++; *v && *v != '"' && i + 1 < n; v++) {
    if (*v == '\\' && v[1])
      v++;
    dst[i++] = *v;
  }
  dst[i] = '\0';
  return true;
}

static long jint(char const* s, char const* key, long dflt)
{
  char const* v = jfind(s, key);
  if (!v || !(isdigit((unsigned char)*v) || *v == '-'))
    return dflt;
  return strtol(v, NULL, 10);
}

/* Up to max strings of a ["a","b"] array into dst[i] (each n bytes). */
static size_t jstrs(char const* s, char const* key, char (*dst)[96], size_t max)
{
  char const* v = jfind(s, key);
  size_t k = 0;
  if (!v || *v != '[')
    return 0;
  for (v++; *v && *v != ']' && k < max;) {
    if (*v == '"') {
      size_t i = 0;
      for (v++; *v && *v != '"'; v++)
        if (i + 1 < sizeof dst[k])
          dst[k][i++] = *v;
      dst[k++][i] = '\0';
      if (*v)
        v++;
    } else {
      v++;
    }
  }
  return k;
}

/* --- E2 nodes ------------------------------------------------------------- */

static void node_name(global_e2_node_id_t const* id, char* dst, size_t n)
{
  int len = snprintf(dst, n, "%s_%03u_%0*u_%x",
                     NODE_IS_DU(id->type) ? "gnbd" : NODE_IS_CU(id->type) ? "gnbc" : "gnb",
                     id->plmn.mcc, id->plmn.mnc_digit_len, id->plmn.mnc, id->nb_id.nb_id);
  if (id->cu_du_id && len > 0 && (size_t)len < n)
    snprintf(dst + len, n - (size_t)len, "_%lu", (unsigned long)*id->cu_du_id);
}

/* The node named `name` (copied into *node) from a fresh list; false if none. */
static bool find_node(char const* name, e2_node_arr_xapp_t* arr, e2_node_connected_xapp_t** node)
{
  *arr = e2_nodes_xapp_api();
  for (size_t i = 0; i < arr->len; i++) {
    char n[96];
    node_name(&arr->n[i].id, n, sizeof n);
    if (name[0] == '\0' || strcmp(n, name) == 0) {
      *node = &arr->n[i];
      return true;
    }
  }
  return false;
}

static sm_ran_function_t* find_rf(e2_node_connected_xapp_t* node, uint32_t rf_id)
{
  for (size_t i = 0; i < node->len_rf; i++)
    if (node->rf[i].id == rf_id)
      return &node->rf[i];
  return NULL;
}

static void op_nodes(void)
{
  e2_node_arr_xapp_t arr = e2_nodes_xapp_api();
  sbuf_t b = {0};
  sb_printf(&b, "{\"op\":\"nodes\",\"nodes\":[");
  for (size_t i = 0; i < arr.len; i++) {
    char n[96];
    node_name(&arr.n[i].id, n, sizeof n);
    sb_printf(&b, i ? "," : "");
    sb_str(&b, n);
  }
  sb_printf(&b, "]}");
  emit(&b);
  free_e2_node_arr_xapp(&arr);
}

/* --- KPM -------------------------------------------------------------------- */

typedef struct {
  bool used;
  char id[64];
  char node[96];
  int handle;
  long period_ms;
} slot_t;

static slot_t slots[MAX_SLOTS];
static pthread_mutex_t slots_mtx = PTHREAD_MUTEX_INITIALIZER;

static void sb_record(sbuf_t* b, meas_record_lst_t const* r)
{
  if (r->value == INTEGER_MEAS_VALUE)
    sb_printf(b, "%lld", (long long)r->int_val);
  else if (r->value == REAL_MEAS_VALUE)
    sb_printf(b, "%.17g", r->real_val);
  else
    sb_printf(b, "null");
}

/* {"<meas name>": [value per granularity period], ...}; first label only. */
static void sb_meas_data(sbuf_t* b, kpm_ind_msg_format_1_t const* m)
{
  sb_printf(b, "{");
  size_t col = 0;
  for (size_t i = 0; i < m->meas_info_lst_len; i++) {
    meas_info_format_1_lst_t const* info = &m->meas_info_lst[i];
    char* name = info->meas_type.type == NAME_MEAS_TYPE ? cp_ba_to_str(info->meas_type.name) : NULL;
    sb_printf(b, i ? "," : "");
    sb_str(b, name ? name : "unnamed");
    free(name);
    sb_printf(b, ":[");
    for (size_t j = 0; j < m->meas_data_lst_len; j++) {
      meas_data_lst_t const* d = &m->meas_data_lst[j];
      sb_printf(b, j ? "," : "");
      if (col < d->meas_record_len)
        sb_record(b, &d->meas_record_lst[col]);
      else
        sb_printf(b, "null");
    }
    sb_printf(b, "]");
    col += info->label_info_lst_len ? info->label_info_lst_len : 1;
  }
  sb_printf(b, "}");
}

static void ue_key(ue_id_e2sm_t const* ue, char* dst, size_t n)
{
  if (ue->type == GNB_DU_UE_ID_E2SM)
    snprintf(dst, n, "%u", ue->gnb_du.gnb_cu_ue_f1ap);
  else if (ue->type == GNB_UE_ID_E2SM && ue->gnb.gnb_cu_ue_f1ap_lst != NULL && ue->gnb.gnb_cu_ue_f1ap_lst_len > 0)
    snprintf(dst, n, "%u", ue->gnb.gnb_cu_ue_f1ap_lst[0]);
  else if (ue->type == GNB_UE_ID_E2SM)
    snprintf(dst, n, "%lu", (unsigned long)ue->gnb.amf_ue_ngap_id);
  else
    snprintf(dst, n, "type%d", (int)ue->type);
}

static void on_indication(int k, sm_ag_if_rd_t const* rd)
{
  if (rd == NULL || rd->type != INDICATION_MSG_AGENT_IF_ANS_V0 || rd->ind.type != KPM_STATS_V3_0)
    return;
  slot_t s;
  pthread_mutex_lock(&slots_mtx);
  s = slots[k];
  pthread_mutex_unlock(&slots_mtx);
  if (!s.used)
    return;

  kpm_ind_data_t const* ind = &rd->ind.kpm.ind;
  sbuf_t b = {0};
  sb_printf(&b, "{\"op\":\"indication\",\"id\":");
  sb_str(&b, s.id);
  sb_printf(&b, ",\"indication\":{\"e2_node_id\":");
  sb_str(&b, s.node);
  /* collectStartTime is in microseconds (FlexRIC compares it with time_now_us). */
  sb_printf(&b, ",\"collect_start_ns\":%lld,\"meas\":{",
            (long long)ind->hdr.kpm_ric_ind_hdr_format_1.collectStartTime * 1000LL);
  if (ind->msg.type == FORMAT_1_INDICATION_MESSAGE) {
    sb_printf(&b, "\"measData\":");
    sb_meas_data(&b, &ind->msg.frm_1);
    sb_printf(&b, ",\"granulPeriod\":%ld", s.period_ms);
  } else if (ind->msg.type == FORMAT_3_INDICATION_MESSAGE) {
    sb_printf(&b, "\"ueMeasData\":{");
    kpm_ind_msg_format_3_t const* m3 = &ind->msg.frm_3;
    for (size_t i = 0; i < m3->ue_meas_report_lst_len; i++) {
      char key[32];
      ue_key(&m3->meas_report_per_ue[i].ue_meas_report_lst, key, sizeof key);
      sb_printf(&b, i ? "," : "");
      sb_str(&b, key);
      sb_printf(&b, ":{\"measData\":");
      sb_meas_data(&b, &m3->meas_report_per_ue[i].ind_msg_format_1);
      sb_printf(&b, ",\"granulPeriod\":%ld}", s.period_ms);
    }
    sb_printf(&b, "}");
  }
  sb_printf(&b, "}}}");
  emit(&b);
}

#define SLOT_CB(k) \
  static void slot_cb_##k(sm_ag_if_rd_t const* rd) { on_indication(k, rd); }
SLOT_CB(0) SLOT_CB(1) SLOT_CB(2) SLOT_CB(3) SLOT_CB(4) SLOT_CB(5) SLOT_CB(6) SLOT_CB(7)
SLOT_CB(8) SLOT_CB(9) SLOT_CB(10) SLOT_CB(11) SLOT_CB(12) SLOT_CB(13) SLOT_CB(14) SLOT_CB(15)
static sm_cb const slot_cbs[MAX_SLOTS] = {
    slot_cb_0, slot_cb_1, slot_cb_2,  slot_cb_3,  slot_cb_4,  slot_cb_5,  slot_cb_6,  slot_cb_7,
    slot_cb_8, slot_cb_9, slot_cb_10, slot_cb_11, slot_cb_12, slot_cb_13, slot_cb_14, slot_cb_15,
};

static label_info_lst_t no_label(void)
{
  label_info_lst_t l = {0};
  l.noLabel = calloc(1, sizeof(enum_value_e));
  *l.noLabel = TRUE_ENUM_VALUE;
  return l;
}

static kpm_act_def_format_1_t act_def_frm_1(char (*metrics)[96], size_t n, long period_ms)
{
  kpm_act_def_format_1_t f = {0};
  f.meas_info_lst_len = n;
  f.meas_info_lst = calloc(n, sizeof(meas_info_format_1_lst_t));
  for (size_t i = 0; i < n; i++) {
    f.meas_info_lst[i].meas_type.type = NAME_MEAS_TYPE;
    f.meas_info_lst[i].meas_type.name = cp_str_to_ba(metrics[i]);
    f.meas_info_lst[i].label_info_lst_len = 1;
    f.meas_info_lst[i].label_info_lst = calloc(1, sizeof(label_info_lst_t));
    f.meas_info_lst[i].label_info_lst[0] = no_label();
  }
  f.gran_period_ms = (uint32_t)period_ms;
  f.cell_global_id = NULL;
  f.meas_bin_range_info_lst_len = 0;
  f.meas_bin_info_lst = NULL;
  return f;
}

/* Style 4's condition: UEs whose S-NSSAI has this SST. */
static test_info_lst_t sst_condition(int sst)
{
  test_info_lst_t t = {0};
  t.test_cond_type = S_NSSAI_TEST_COND_TYPE;
  t.S_NSSAI = TRUE_TEST_COND_TYPE;
  t.test_cond = calloc(1, sizeof(test_cond_e));
  *t.test_cond = EQUAL_TEST_COND;
  t.test_cond_value = calloc(1, sizeof(test_cond_value_t));
  t.test_cond_value->type = OCTET_STRING_TEST_COND_VALUE;
  t.test_cond_value->octet_string_value = calloc(1, sizeof(byte_array_t));
  t.test_cond_value->octet_string_value->len = 1;
  t.test_cond_value->octet_string_value->buf = calloc(1, 1);
  t.test_cond_value->octet_string_value->buf[0] = (uint8_t)sst;
  return t;
}

/* NULL if the node advertises `style` with every metric; else why not. */
static char const* check_style(sm_ran_function_t* rf, long style, char (*metrics)[96], size_t n,
                               char* why, size_t why_n)
{
  if (rf->defn.type != KPM_RAN_FUNC_DEF_E) {
    snprintf(why, why_n, "RAN function %u is not E2SM-KPM", (unsigned)rf->id);
    return why;
  }
  kpm_ran_function_def_t const* d = &rf->defn.kpm;
  for (size_t i = 0; i < d->sz_ric_report_style_list; i++) {
    ric_report_style_item_t const* it = &d->ric_report_style_list[i];
    if ((long)it->report_style_type + 1 != style)
      continue;
    for (size_t m = 0; m < n; m++) {
      bool found = false;
      for (size_t a = 0; a < it->meas_info_for_action_lst_len && !found; a++)
        found = cmp_str_ba(metrics[m], it->meas_info_for_action_lst[a].name) == 0;
      if (!found) {
        snprintf(why, why_n, "the E2 node does not offer %s in report style %ld", metrics[m], style);
        return why;
      }
    }
    return NULL;
  }
  snprintf(why, why_n, "the E2 node does not offer KPM report style %ld", style);
  return why;
}

static void op_subscribe(char const* line)
{
  char id[64] = "", node_req[96] = "";
  char metrics[64][96];
  jstr(line, "id", id, sizeof id);
  jstr(line, "node", node_req, sizeof node_req);
  long style = jint(line, "style", 4);
  long period = jint(line, "period_ms", 1000);
  int sst = (int)jint(line, "sst", 1);
  size_t n = jstrs(line, "metrics", metrics, 64);
  char why[256];

  if (style != 1 && style != 4) {
    snprintf(why, sizeof why,
             "KPM report style %ld is not built by mec-cast-e2-bridge: use 4 (per UE) or 1 (node level)",
             style);
    emit_simple("subscribe_failed", id, "detail", why);
    return;
  }
  if (n == 0) {
    emit_simple("subscribe_failed", id, "detail", "no metrics requested");
    return;
  }

  e2_node_arr_xapp_t arr = {0};
  e2_node_connected_xapp_t* node = NULL;
  if (!find_node(node_req, &arr, &node)) {
    snprintf(why, sizeof why, "no connected E2 node %s", node_req[0] ? node_req : "at all");
    emit_simple("subscribe_failed", id, "detail", why);
    free_e2_node_arr_xapp(&arr);
    return;
  }
  sm_ran_function_t* rf = find_rf(node, KPM_RAN_FUNC_ID);
  if (rf == NULL || check_style(rf, style, metrics, n, why, sizeof why) != NULL) {
    if (rf == NULL)
      snprintf(why, sizeof why, "the E2 node offers no E2SM-KPM (RAN function %d)", KPM_RAN_FUNC_ID);
    emit_simple("subscribe_failed", id, "detail", why);
    free_e2_node_arr_xapp(&arr);
    return;
  }

  int k = -1;
  pthread_mutex_lock(&slots_mtx);
  for (int i = 0; i < MAX_SLOTS && k < 0; i++)
    if (!slots[i].used)
      k = i;
  if (k >= 0) {
    slots[k] = (slot_t){.used = true, .period_ms = period};
    snprintf(slots[k].id, sizeof slots[k].id, "%s", id);
    node_name(&node->id, slots[k].node, sizeof slots[k].node);
  }
  pthread_mutex_unlock(&slots_mtx);
  if (k < 0) {
    emit_simple("subscribe_failed", id, "detail", "too many subscriptions");
    free_e2_node_arr_xapp(&arr);
    return;
  }

  kpm_sub_data_t sub = {0};
  sub.ev_trg_def.type = FORMAT_1_RIC_EVENT_TRIGGER;
  sub.ev_trg_def.kpm_ric_event_trigger_format_1.report_period_ms = (uint32_t)period;
  sub.sz_ad = 1;
  sub.ad = calloc(1, sizeof(kpm_act_def_t));
  if (style == 1) {
    sub.ad[0].type = FORMAT_1_ACTION_DEFINITION;
    sub.ad[0].frm_1 = act_def_frm_1(metrics, n, period);
  } else {
    sub.ad[0].type = FORMAT_4_ACTION_DEFINITION;
    sub.ad[0].frm_4.matching_cond_lst_len = 1;
    sub.ad[0].frm_4.matching_cond_lst = calloc(1, sizeof(matching_condition_format_4_lst_t));
    sub.ad[0].frm_4.matching_cond_lst[0].test_info_lst = sst_condition(sst);
    sub.ad[0].frm_4.action_def_format_1 = act_def_frm_1(metrics, n, period);
  }

  sm_ans_xapp_t ans = report_sm_xapp_api(&node->id, KPM_RAN_FUNC_ID, &sub, slot_cbs[k]);
  free_kpm_sub_data(&sub);
  free_e2_node_arr_xapp(&arr);
  if (!ans.success) {
    pthread_mutex_lock(&slots_mtx);
    slots[k].used = false;
    pthread_mutex_unlock(&slots_mtx);
    emit_simple("subscribe_failed", id, "detail", ans.u.reason ? ans.u.reason : "refused");
    return;
  }
  pthread_mutex_lock(&slots_mtx);
  slots[k].handle = ans.u.handle;
  pthread_mutex_unlock(&slots_mtx);
  emit_simple("subscribed", id, NULL, NULL);
}

static void op_unsubscribe(char const* line)
{
  char id[64] = "";
  jstr(line, "id", id, sizeof id);
  int handle = -1;
  pthread_mutex_lock(&slots_mtx);
  for (int i = 0; i < MAX_SLOTS; i++)
    if (slots[i].used && strcmp(slots[i].id, id) == 0) {
      handle = slots[i].handle;
      slots[i].used = false;
    }
  pthread_mutex_unlock(&slots_mtx);
  if (handle >= 0)
    rm_report_sm_xapp_api(handle);
}

/* --- RC: slice-level PRB quota (Control Style 2, Action 6) ------------------ */

static ran_parameter_value_t* elem_int(int64_t v)
{
  ran_parameter_value_t* p = calloc(1, sizeof *p);
  p->type = INTEGER_RAN_PARAMETER_VALUE;
  p->int_ran = v;
  return p;
}

static ran_parameter_value_t* elem_octets(uint8_t const* bytes, size_t n)
{
  ran_parameter_value_t* p = calloc(1, sizeof *p);
  p->type = OCTET_STRING_RAN_PARAMETER_VALUE;
  p->octet_str_ran.len = n;
  p->octet_str_ran.buf = calloc(n, 1);
  memcpy(p->octet_str_ran.buf, bytes, n);
  return p;
}

static seq_ran_param_t p_elem(uint32_t id, ran_parameter_value_t* v)
{
  seq_ran_param_t p = {.ran_param_id = id};
  p.ran_param_val.type = ELEMENT_KEY_FLAG_FALSE_RAN_PARAMETER_VAL_TYPE;
  p.ran_param_val.flag_false = v;
  return p;
}

static seq_ran_param_t p_struct(uint32_t id, size_t n, seq_ran_param_t* members)
{
  seq_ran_param_t p = {.ran_param_id = id};
  p.ran_param_val.type = STRUCTURE_RAN_PARAMETER_VAL_TYPE;
  p.ran_param_val.strct = calloc(1, sizeof(ran_param_struct_t));
  p.ran_param_val.strct->sz_ran_param_struct = n;
  p.ran_param_val.strct->ran_param_struct = members;
  return p;
}

/* A list of one item; the item is a structure of `members` (items carry no id). */
static seq_ran_param_t p_list1(uint32_t id, size_t n, seq_ran_param_t* members)
{
  seq_ran_param_t p = {.ran_param_id = id};
  p.ran_param_val.type = LIST_RAN_PARAMETER_VAL_TYPE;
  p.ran_param_val.lst = calloc(1, sizeof(ran_param_list_t));
  p.ran_param_val.lst->sz_lst_ran_param = 1;
  p.ran_param_val.lst->lst_ran_param = calloc(1, sizeof(lst_ran_param_t));
  p.ran_param_val.lst->lst_ran_param[0].ran_param_struct.sz_ran_param_struct = n;
  p.ran_param_val.lst->lst_ran_param[0].ran_param_struct.ran_param_struct = members;
  return p;
}

static seq_ran_param_t* seq(size_t n)
{
  return calloc(n, sizeof(seq_ran_param_t));
}

/* PLMN "00101" / "001001" -> the 3-byte TBCD of TS 38.413 (filler F for a 2-digit MNC). */
static bool plmn_tbcd(char const* plmn, uint8_t out3[3])
{
  size_t len = strlen(plmn);
  if (len != 5 && len != 6)
    return false;
  int d[6];
  for (size_t i = 0; i < len; i++) {
    if (!isdigit((unsigned char)plmn[i]))
      return false;
    d[i] = plmn[i] - '0';
  }
  int mnc3 = len == 6 ? d[5] : 0xF;
  out3[0] = (uint8_t)((d[1] << 4) | d[0]);
  out3[1] = (uint8_t)((mnc3 << 4) | d[2]);
  out3[2] = (uint8_t)((d[4] << 4) | d[3]);
  return true;
}

/*
 *  1 RRM Policy Ratio List (LIST)
 *    item: 2 RRM Policy Ratio Group (STRUCTURE)
 *            3 RRM Policy (STRUCTURE)
 *                5 RRM Policy Member List (LIST)
 *                  item: 6 RRM Policy Member (STRUCTURE)
 *                          7 PLMN Identity (octets, TBCD)
 *                          8 S-NSSAI (STRUCTURE): 9 SST (1 octet), 10 SD (3 octets)
 *           11 Min, 12 Max, 13 Dedicated PRB Policy Ratio (integers, percent)
 */
static rc_ctrl_req_data_t prb_quota_request(uint32_t ue_f1ap, char const* plmn_bytes_src, int sst, uint32_t sd,
                                            int min_r, int max_r, int ded_r)
{
  uint8_t plmn[3];
  memcpy(plmn, plmn_bytes_src, 3);
  uint8_t sst_b = (uint8_t)sst;
  uint8_t sd_b[3] = {(uint8_t)(sd >> 16), (uint8_t)(sd >> 8), (uint8_t)sd};

  seq_ran_param_t* snssai = seq(2);
  snssai[0] = p_elem(9, elem_octets(&sst_b, 1));
  snssai[1] = p_elem(10, elem_octets(sd_b, 3));

  seq_ran_param_t* member = seq(2);
  member[0] = p_elem(7, elem_octets(plmn, 3));
  member[1] = p_struct(8, 2, snssai);

  seq_ran_param_t* member_lst = seq(1);
  member_lst[0] = p_struct(6, 2, member);

  seq_ran_param_t* policy = seq(1);
  policy[0] = p_list1(5, 1, member_lst);

  seq_ran_param_t* group = seq(4);
  group[0] = p_struct(3, 1, policy);
  group[1] = p_elem(11, elem_int(min_r));
  group[2] = p_elem(12, elem_int(max_r));
  group[3] = p_elem(13, elem_int(ded_r));

  seq_ran_param_t* group_item = seq(1);
  group_item[0] = p_struct(2, 4, group);

  rc_ctrl_req_data_t req = {0};
  req.hdr.format = FORMAT_1_E2SM_RC_CTRL_HDR;
  req.hdr.frmt_1.ue_id.type = GNB_DU_UE_ID_E2SM;
  req.hdr.frmt_1.ue_id.gnb_du.gnb_cu_ue_f1ap = ue_f1ap;
  req.hdr.frmt_1.ric_style_type = 2; /* Radio Resource Allocation Control */
  req.hdr.frmt_1.ctrl_act_id = 6;    /* Slice-level PRB quota */
  req.msg.format = FORMAT_1_E2SM_RC_CTRL_MSG;
  req.msg.frmt_1.sz_ran_param = 1;
  req.msg.frmt_1.ran_param = seq(1);
  req.msg.frmt_1.ran_param[0] = p_list1(1, 1, group_item);
  return req;
}

static void op_control(char const* line)
{
  char id[64] = "", node_req[96] = "", action[32] = "", plmn_s[8] = "00101";
  jstr(line, "id", id, sizeof id);
  jstr(line, "node", node_req, sizeof node_req);
  jstr(line, "action", action, sizeof action);
  jstr(line, "plmn", plmn_s, sizeof plmn_s);

  sbuf_t b = {0};
  char why[256] = "";
  bool ok = false;
  uint8_t plmn[3];

  e2_node_arr_xapp_t arr = {0};
  e2_node_connected_xapp_t* node = NULL;
  if (strcmp(action, "prb_quota") != 0) {
    snprintf(why, sizeof why, "unknown control action %s", action);
  } else if (!plmn_tbcd(plmn_s, plmn)) {
    snprintf(why, sizeof why, "PLMN %s is not 5 or 6 digits", plmn_s);
  } else if (!find_node(node_req, &arr, &node)) {
    snprintf(why, sizeof why, "no connected E2 node %s", node_req);
  } else if (find_rf(node, RC_RAN_FUNC_ID) == NULL) {
    snprintf(why, sizeof why, "the E2 node offers no E2SM-RC (RAN function %d)", RC_RAN_FUNC_ID);
  } else {
    rc_ctrl_req_data_t req = prb_quota_request(
        (uint32_t)jint(line, "ue_id", 0), (char const*)plmn, (int)jint(line, "sst", 1),
        (uint32_t)jint(line, "sd", 1), (int)jint(line, "min_prb_ratio", 0),
        (int)jint(line, "max_prb_ratio", 100), (int)jint(line, "dedicated_prb_ratio", 0));

    pthread_mutex_lock(&ctl_mtx);
    ctl_answer = 0;
    pthread_mutex_unlock(&ctl_mtx);

    control_sm_xapp_api(&node->id, RC_RAN_FUNC_ID, &req); /* waits up to ~6 s */
    free_rc_ctrl_req_data(&req);

    /* The ack/failure line may still be in the pipe when the call returns. */
    struct timespec until;
    clock_gettime(CLOCK_REALTIME, &until);
    until.tv_sec += 1;
    pthread_mutex_lock(&ctl_mtx);
    while (ctl_answer == 0 && pthread_cond_timedwait(&ctl_cv, &ctl_mtx, &until) == 0) {
    }
    int answer = ctl_answer;
    pthread_mutex_unlock(&ctl_mtx);

    ok = answer == 1;
    if (answer == -1)
      snprintf(why, sizeof why, "RIC_CONTROL_FAILURE");
    else if (answer == 0)
      snprintf(why, sizeof why, "no RIC Control Acknowledge or Failure within FlexRIC's 5 s");
  }
  free_e2_node_arr_xapp(&arr);

  sb_printf(&b, "{\"op\":\"control_ack\",\"id\":");
  sb_str(&b, id);
  sb_printf(&b, ",\"ok\":%s,\"detail\":", ok ? "true" : "false");
  sb_str(&b, why);
  sb_printf(&b, "}");
  emit(&b);
}

/* --- main ----------------------------------------------------------------------- */

int main(int argc, char* argv[])
{
  split_stdout();
  signal(SIGPIPE, SIG_IGN);

  fr_args_t args = init_fr_args(argc, argv);
  init_xapp_api(&args);
  sleep(1); /* FlexRIC's own examples wait for the E42 setup to settle */
  emit_simple("log", NULL, "detail", "mec-cast-e2-bridge ready");

  char* line = malloc(MAX_LINE);
  while (fgets(line, MAX_LINE, stdin)) {
    char op[32] = "";
    if (!jstr(line, "op", op, sizeof op))
      continue;
    if (strcmp(op, "nodes") == 0)
      op_nodes();
    else if (strcmp(op, "subscribe") == 0)
      op_subscribe(line);
    else if (strcmp(op, "unsubscribe") == 0)
      op_unsubscribe(line);
    else if (strcmp(op, "control") == 0)
      op_control(line);
  }
  free(line);

  /* stdin closed: the xApp is stopping. Leave nothing subscribed. */
  for (int i = 0; i < MAX_SLOTS; i++)
    if (slots[i].used) {
      slots[i].used = false;
      rm_report_sm_xapp_api(slots[i].handle);
    }
  while (!try_stop_xapp_api())
    usleep(1000);
  return 0;
}
