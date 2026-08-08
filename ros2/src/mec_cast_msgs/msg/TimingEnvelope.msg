# mec-cast timing envelope — ROS carrier of the shared 64-byte wire contract
# (see telemetry/src/envelope.rs). Timestamps are CLOCK_REALTIME nanoseconds
# on the PTP-disciplined time base; 0 means "not stamped yet".

int64 capture_ns        # sensor delivered the data
int64 send_ns           # last stamp before leaving the sender process
int64 recv_ns           # first stamp after arriving at the receiver process
int64 process_done_ns   # receiver-side processing finished
uint64 seq              # per-publisher monotonic sequence number
uint8 modality          # 0=pointcloud 1=video 2=audio 3=generic
uint8[16] trace_id      # experiment run id (UUID bytes)
