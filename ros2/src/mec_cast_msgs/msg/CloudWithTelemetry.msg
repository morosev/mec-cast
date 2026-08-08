# A point cloud with its timing envelope riding in-band.
# rmw_zenoh does not expose per-publish Zenoh attachments to the application
# layer, so the envelope travels inside the message; the binary wire format
# (TimingEnvelope) stays the cross-profile contract.

TimingEnvelope envelope
sensor_msgs/PointCloud2 cloud
