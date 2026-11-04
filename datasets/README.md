# Recorded lidar datasets

The lidar client replays a recorded dataset with `PATTERN=dataset`, from a
directory here in the replay format `tools/lidar_dataset.py` writes. Nothing
but this README is tracked: the data is the dataset's, under its own terms,
and gigabytes in size. Git, the docker build context and `deploy.sh` all leave
this directory's contents out; the UE containers mount it read-only at
`/datasets`.

Convert one:

```bash
python3 tools/lidar_dataset.py ford ~/IJRR-Dataset-1/SCANS datasets/ford-campus-1
```

Replay it:

```bash
PATTERN=dataset DATASET=ford-campus-1 make up-admin
```

In the lab, copy the converted directory to the same place on each UE host,
since the deploy does not carry it:

```bash
rsync -a datasets/ford-campus-1 ops@ue-host:mec-cast/datasets/
```

| Dataset | Source | Frames | Points a frame | Size converted |
|---|---|---|---|---|
| `ford-campus-1` | Ford Campus Vision and Lidar Dataset, run 1 (Pandey, McBride and Eustice, IJRR 2011): Velodyne HDL-64E, vehicle frame, 10 Hz | 3,817 (381 s) | 74,000-89,000 | 3.6 GB |
