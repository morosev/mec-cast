/*
 * C ABI smoke test for mec-cast-telemetry.
 *
 * Compiled as C (not C++) on purpose: it proves the public header is
 * C-clean and that the staticlib links and runs from a foreign toolchain —
 * the exact contract clients/webrtc_native/build.sh depends on.
 *
 * This matters because the legacy WebRTC addon's own video path cannot be
 * exercised in CI or in WSL (no camera, no /dev/video*), so without this
 * the C boundary would ship untested.
 *
 * Run with: make test-ffi
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "mec_cast_telemetry.h"

#define CHECK(cond, msg)                             \
  do {                                               \
    if (!(cond)) {                                   \
      fprintf(stderr, "FAIL: %s (%s:%d)\n", msg,     \
              __FILE__, __LINE__);                   \
      return 1;                                      \
    }                                                \
  } while (0)

#define SAMPLES 100

int main(void) {
  const char* out_dir = "target/c_abi_smoke_run";
  char csv_path[512];
  snprintf(csv_path, sizeof(csv_path), "%s/samples.csv", out_dir);
  remove(csv_path);

  /* NULL arguments must fail cleanly rather than crash. */
  CHECK(mct_recorder_start(NULL, NULL, NULL, NULL, 0.0) == NULL,
        "NULL args should return NULL");
  CHECK(!mct_record(NULL, MCT_MODALITY_VIDEO, 0, 0, 0, 0, 0, 0, 0, 0),
        "record on NULL handle should return false");
  CHECK(mct_dropped_total(NULL) == 0, "dropped on NULL should be 0");
  mct_recorder_stop(NULL, NULL); /* must not crash */

  MctRecorder* rec = mct_recorder_start("c-abi-smoke", "mec-cast-media",
                                        out_dir, NULL, 0.0);
  CHECK(rec != NULL, "recorder should start");

  /* Timestamps shaped like a real frame: 5ms network, 30ms glass-to-glass. */
  for (int i = 0; i < SAMPLES; i++) {
    long long capture = 1000000000LL + (long long)i * 33333333LL;
    int ok = mct_record(rec, MCT_MODALITY_VIDEO, (unsigned long long)i,
                        capture,               /* capture_ns      */
                        capture + 20000000LL,  /* send_ns         */
                        capture + 25000000LL,  /* recv_ns         */
                        capture + 30000000LL,  /* process_done_ns */
                        460800,                /* payload_bytes   */
                        4000000LL,             /* aux_ns (decode) */
                        1);                    /* site            */
    CHECK(ok, "sample should be accepted");
  }

  CHECK(!mct_record(rec, 200, 0, 0, 0, 0, 0, 0, 0, 0),
        "unknown modality must be rejected, not aborted");

  MctReport report;
  memset(&report, 0, sizeof(report));
  mct_recorder_stop(rec, &report);

  CHECK(report.samples_written == SAMPLES, "all samples written");
  CHECK(report.samples_dropped == 0, "no drops at this rate");

  FILE* f = fopen(csv_path, "r");
  CHECK(f != NULL, "samples.csv should exist");

  char line[1024];
  CHECK(fgets(line, sizeof(line), f) != NULL, "header readable");
  CHECK(strstr(line, "capture_ns") != NULL, "header has capture_ns");
  CHECK(strstr(line, "e2e_ns") != NULL, "header has derived e2e_ns");

  int rows = 0;
  while (fgets(line, sizeof(line), f) != NULL) {
    if (rows == 0) {
      CHECK(strstr(line, "video") != NULL, "modality written as video");
      /* network = recv - send = 5ms; e2e = done - capture = 30ms */
      CHECK(strstr(line, "5000000") != NULL, "network_ns == 5ms");
      CHECK(strstr(line, "30000000") != NULL, "e2e_ns == 30ms");
    }
    rows++;
  }
  fclose(f);
  CHECK(rows == SAMPLES, "CSV row count matches");

  printf("ok: %d samples through the C ABI, CSV verified\n", rows);
  return 0;
}
