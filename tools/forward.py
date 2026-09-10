#!/usr/bin/env python3
"""Forward a local TCP port to a device the manager can reach but you cannot.

For the case where a receiver holds an address from another subnet: the manager
container sits on the TV VLAN and can talk to it, but your browser is on a
different network. Rather than reconfiguring a laptop's VLAN and adding a
secondary address, run this in the container and browse to the container.

    # in the container, which already has a leg on the device's subnet
    python3 tools/forward.py --to 192.168.1.12:80 --port 8080

    # then, from your desk
    http://<container-ip>:8080/          (admin / admin)

Plain TCP relay, no interpretation, so the web UI works normally. Stop it with
Ctrl-C; it holds nothing open afterwards.

Remember the device still needs an address in *its* subnet to be reachable from
the container:

    ip addr add 192.168.1.240/24 dev eth1
"""

from __future__ import annotations

import argparse
import socket
import socketserver
import sys
import threading

#: Anything larger is pointless for a small embedded web UI.
BUFFER = 32768


class Relay(socketserver.BaseRequestHandler):
    target: tuple[str, int]
    verbose: bool

    def handle(self) -> None:
        client = self.request
        try:
            upstream = socket.create_connection(self.target, timeout=10)
        except OSError as exc:
            if self.verbose:
                print(f"  ✗ cannot reach {self.target[0]}:{self.target[1]}: {exc}")
            client.close()
            return
        if self.verbose:
            print(f"  {self.client_address[0]} -> "
                  f"{self.target[0]}:{self.target[1]}")

        def pump(source: socket.socket, sink: socket.socket) -> None:
            try:
                while True:
                    chunk = source.recv(BUFFER)
                    if not chunk:
                        break
                    sink.sendall(chunk)
            except OSError:
                pass
            finally:
                # Half-close so the other direction can finish cleanly.
                for sock in (source, sink):
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        upward = threading.Thread(target=pump, args=(client, upstream),
                                  daemon=True)
        upward.start()
        pump(upstream, client)
        upward.join(timeout=5)
        for sock in (client, upstream):
            try:
                sock.close()
            except OSError:
                pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--to", required=True, metavar="HOST:PORT",
                        help="where to forward, e.g. 192.168.1.12:80")
    parser.add_argument("--port", type=int, default=8080,
                        help="port to listen on (default 8080)")
    parser.add_argument("--bind", default="0.0.0.0",
                        help="address to listen on (default all)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    host, _, port_text = args.to.rpartition(":")
    if not host or not port_text.isdigit():
        print(f"✗ --to must be HOST:PORT, got {args.to!r}", file=sys.stderr)
        return 2
    target = (host, int(port_text))

    handler = type("Bound", (Relay,), {"target": target,
                                       "verbose": not args.quiet})
    try:
        server = Server((args.bind, args.port), handler)
    except OSError as exc:
        print(f"✗ cannot listen on {args.bind}:{args.port}: {exc}",
              file=sys.stderr)
        return 1

    print(f"→ forwarding {args.bind}:{args.port} -> {target[0]}:{target[1]}")
    addresses = []
    try:
        import subprocess
        out = subprocess.run(["hostname", "-I"], capture_output=True,
                             text=True, timeout=5).stdout.split()
        addresses = [a for a in out if ":" not in a]
    except Exception:
        pass
    for address in addresses:
        print(f"  browse to  http://{address}:{args.port}/")
    print("  Ctrl-C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
