/*
 * mec-cast-telemetry — C ABI.
 *
 * Lets a non-Rust producer feed the shared measurement spine. Used by the
 * legacy WebRTC addon (Profile B) so its media runs land in the same CSV
 * schema and logging service as the ROS2/Zenoh profile.
 *
 * Link against libmec_cast_telemetry.a (cargo build -p mec-cast-telemetry
 * --release produces target/release/libmec_cast_telemetry.a).
 *
 * Threading: a recorder is NOT thread-safe. The queue is single-producer;
 * call mct_record from one thread only.
 *
 * Keep in sync with telemetry/src/ffi.rs.
 */
#ifndef MEC_CAST_TELEMETRY_H_
#define MEC_CAST_TELEMETRY_H_

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct MctRecorder MctRecorder;

/* Envelope modality byte — must match Modality in telemetry/src/envelope.rs. */
#define MCT_MODALITY_POINTCLOUD 0
#define MCT_MODALITY_VIDEO      1
#define MCT_MODALITY_AUDIO      2
#define MCT_MODALITY_GENERIC    3

typedef struct {
  uint64_t samples_written;
  uint64_t samples_dropped;
  uint64_t snapshots_built;
  uint64_t snapshots_posted;
  uint64_t snapshots_dropped;
  uint64_t post_failures;
} MctReport;

/*
 * Start a recorder writing <out_dir>/samples.csv.
 *
 * logging_url may be NULL or "" for CSV only.
 * snapshot_interval_s <= 0 selects the 2 s default.
 * Returns NULL on failure (bad arguments, or out_dir/CSV not creatable).
 */
MctRecorder* mct_recorder_start(const char* run_id,
                                const char* service,
                                const char* out_dir,
                                const char* logging_url,
                                double snapshot_interval_s);

/*
 * Record one sample. Never blocks, never allocates — safe on a media
 * callback. Unstamped timestamps should be passed as 0.
 *
 * Returns false if recorder is NULL, modality is unknown, or the queue was
 * full (that drop is counted and reported).
 */
bool mct_record(MctRecorder* recorder,
                uint8_t modality,
                uint64_t seq,
                int64_t capture_ns,
                int64_t send_ns,
                int64_t recv_ns,
                int64_t process_done_ns,
                uint32_t payload_bytes,
                int64_t aux_ns,
                uint8_t site);

/* Samples dropped so far due to a full queue. 0 when recorder is NULL. */
uint64_t mct_dropped_total(const MctRecorder* recorder);

/*
 * Drain, flush, join background threads, and free the recorder. Writes final
 * counts to *out when out is non-NULL. The handle is invalid afterwards.
 */
void mct_recorder_stop(MctRecorder* recorder, MctReport* out);

#ifdef __cplusplus
}  /* extern "C" */
#endif

#endif  /* MEC_CAST_TELEMETRY_H_ */
