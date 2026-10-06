# Encoding on the GPU (Intel, VAAPI)

x264 on four vCPUs carried three 1080p30 channels, but with little room to
spare. When an announcement video played on two channels at once, the capture
fell behind and ffmpeg dropped nearly every frame for about twenty seconds.
Encoding on an Intel integrated GPU used a quarter of x264's CPU for the same
1080p30 6M stream, measured on the same input.

**Measured in production, a week side by side** (two channels showing the
same site, with the same announcement videos):

| Encoder | Bursts of dropped frames | Frames dropped in bursts |
|---|---|---|
| x264 | 16 windows, about 10 occasions | 3641 |
| VAAPI | 0 | 0 |

Both also dropped one frame roughly every 56 minutes. That is drift between
the capture clock and the output clock, about 10 ppm, and invisible on screen.
The bursts come from `/run/eclerstreamer/progress-<channel>.txt`, which keeps
the encoder's counters since the stream started:

```bash
awk -F= '/^out_time=/{split($2,a,"."); t=a[1]}
  /^drop_frames=/{d=$2-p; p=$2; if (d==1) s++; else if (d>1) {b++; bf+=d; print "  +" t, "dropped", d}}
  END {printf "  hourly drift: %d   burst windows: %d   frames in bursts: %d\n", s, b, bf}' \
  /run/eclerstreamer/progress-6.txt
```

Each line is a 10-second window, timed from the stream's start
(`systemctl show -p ExecMainStartTimestamp dashboard-stream@6`). A restart
starts a new file, so read it first.

Each channel has an **Encoder** setting on the streamer page: *Processor
(x264)* or *Graphics chip (VAAPI)*. It takes effect when that channel
restarts. Move channels over one at a time, and switch one back if its TV
shows trouble.

## What the host needs

Tested with a Proxmox host whose Intel GPU (HD Graphics 630, Kaby Lake) is
passed through whole to the streamer VM. The host runs headless, so it does
not miss the GPU.

**Check first (read-only, on the host):**

```bash
dmesg | grep -iE "IOMMU enabled|Directed I/O"      # IOMMU on in the kernel
ls /sys/kernel/iommu_groups/ | wc -l               # > 0
ls /sys/bus/pci/devices/0000:00:02.0/iommu_group/devices/   # only 0000:00:02.0
```

If the first prints nothing, enable VT-d in the BIOS.

**Reserve the GPU for passthrough** (one host reboot). Replace `8086:5912`
with your GPU's ID from `lspci -nn -s 00:02.0`:

```bash
cat > /etc/modprobe.d/igpu-passthrough.conf <<'EOF'
blacklist i915
options vfio-pci ids=8086:5912
EOF
printf 'vfio\nvfio_iommu_type1\nvfio_pci\n' >> /etc/modules
update-initramfs -u -k all
reboot
```

Afterwards `lspci -nnk -s 00:02.0` shows `Kernel driver in use: vfio-pci`.

**Attach it to the VM** (a VM restart; `q35` machine type):

```bash
qm shutdown <vmid>
qm set <vmid> -hostpci0 0000:00:02.0
qm start <vmid>
```

To undo: `qm set <vmid> --delete hostpci0`, remove the modprobe file and the
three `vfio` lines, `update-initramfs -u -k all`, reboot.

## What the VM needs

On Debian 13, H.264 *encoding* on this generation needs the non-free Intel
driver. Add `non-free` to the apt sources, then:

```bash
apt-get install -y vainfo intel-media-va-driver-non-free
vainfo --display drm --device /dev/dri/renderD128 | grep H264
```

Look for `VAProfileH264Main : VAEntrypointEncSlice`. A quick encode, sending
nothing anywhere:

```bash
ffmpeg -hide_banner -vaapi_device /dev/dri/renderD128 \
  -f lavfi -i testsrc=size=1920x1080:rate=30 -t 10 \
  -vf 'format=nv12,hwupload' -c:v h264_vaapi -b:v 6M -f null -
```

A `speed` well above 1x is the answer. The HD 630 managed 3x including the
CPU cost of generating the test picture.

The stream units already allow it: `dashboard-stream@.service` keeps a closed
device policy, admits only DRM render nodes (`DeviceAllow=char-drm rw`) and
runs with the `render` group.

## Watching the load

The streamer page shows **CPU**, **RAM** and **GPU** bars in its header, and
each channel on the GPU gets a `gpu %` chip. The GPU figure is the busier of
the video engine (encoding) and the render engine (colour conversion); the
tooltip shows both and the GPU clock.

None of it needs root. CPU and memory come from `/proc/stat` and
`/proc/meminfo`. For the GPU, the i915 driver publishes in
`/proc/<pid>/fdinfo` how long each engine has been busy for every process
with the GPU open, and a service may read that for processes of its own
account, which the stream units are. `intel_gpu_top` shows the same engines
as root, if you ever want a second opinion.

## What changes in the stream

Nothing a receiver can tell apart: H.264 Main at level 4.0, a keyframe every
1.5 s, no B-frames, the same `qmin` floor against oversized keyframes,
constant rate, and the same transport-stream padding and network pacing. The
colour conversion from the captured picture happens on the GPU as well
(`hwupload,scale_vaapi=format=nv12`), otherwise the CPU would spend a good
part of what the GPU saves.

What cannot be known from here is how this encoder's rate control looks on a
wall in practice. That is why the setting is per channel: try one, watch a
liftoff on its TV, then move the rest.
