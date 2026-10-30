# ingress-throttle

Temporarily throttle inbound bandwidth on a network interface.

Inbound packets are redirected to a dedicated IFB device, where a cake qdisc
shapes them to the given rate. cake shares the rate fairly between flows, so
small flows are not stuck behind bulk transfers.

Nothing is persisted: the settings disappear on reboot.

## Usage

```
$ sudo ingress-throttle.py on -i eth0 -r 300mbit
+ modprobe ifb numifbs=0
+ ip link add name ifbthrottle0 type ifb
+ ip link set dev ifbthrottle0 up
+ tc qdisc replace dev ifbthrottle0 root cake bandwidth 300mbit besteffort ingress
+ tc qdisc add dev eth0 handle ffff: ingress
+ tc filter replace dev eth0 ingress pref 49152 handle 1 protocol all flower action mirred egress redirect dev ifbthrottle0

$ sudo ingress-throttle.py off -i eth0
+ tc filter del dev eth0 ingress pref 49152
+ tc qdisc del dev eth0 ingress
+ ip link del dev ifbthrottle0
```

`status -i eth0` shows the redirect filter and cake's statistics on the IFB
device.

The rate is in tc syntax: `mbit` is megabits per second, while `mbps` is
megabytes per second.

`on` can be run again to change the rate; the throttle stays in place in
between. `on` and `off` print each command they run; `--dry-run` prints them
without running them.

The tool only touches what it owns: its redirect filter (pref 49152) and the
IFB device `ifbthrottle0`. An existing ingress or clsact qdisc is used as is,
other ingress filters are left alone, and `off` removes the ingress qdisc only
when it is a plain `ingress` qdisc with no other filters. A device of another
type with the IFB name is never used or deleted.

## Checking the effect

Only inbound traffic is shaped, so measure traffic arriving at the throttled
host. On a peer:

```
iperf3 -s
irtt server
```

On the throttled host:

```
sudo ingress-throttle.py on -i eth0 -r 100mbit
irtt client -d 15s 10.0.0.12 &
iperf3 -c 10.0.0.12 -R -t 10
sudo ingress-throttle.py status -i eth0
sudo ingress-throttle.py off -i eth0
```

iperf3 reports a rate slightly below the limit, as it counts payload only.
Without `-R` it measures the outbound direction, which is not shaped. irtt
shows that latency stays low while the rate is saturated.

## Requires

- Linux with `ifb`, `sch_ingress`, `sch_cake`, `act_mirred` and `cls_flower`
- iproute2 (`ip`, `tc`) with JSON output
- Python 3.8 or later
- root

## License

This project is licensed under the [MIT License](./LICENSE).
