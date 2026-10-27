# A point cloud coded with a point-cloud codec, its timing envelope riding
# in-band as in CloudWithTelemetry (ADR-0014). Carried on its own topics:
# mec_cast/cloud_gpcc (uplink) and mec_cast/result_gpcc (downlink).

TimingEnvelope envelope
std_msgs/Header header
string codec            # "gpcc"
uint32 num_points       # points in the frame, before coding
float32 precision_m     # position step of the coded metres; 0 = integer indices, lossless
float32 voxel_size_m    # > 0: the points are voxel indices of this edge length (downlink)
uint8[] data            # one coded frame, self-contained: it carries its parameter sets
