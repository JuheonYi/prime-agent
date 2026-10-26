"""Sandbox-local TCP to fixed Unix sockets. This process has no external network."""

import argparse
import select
import socket
import socketserver
import subprocess
import threading


class Relay(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port, target):
        self.target = target
        super().__init__(("127.0.0.1", port), Forward)


class Forward(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
            upstream.connect(self.server.target)
            peers = {self.request: upstream, upstream: self.request}
            while peers:
                readable, _, _ = select.select(list(peers), [], [], 1)
                for source in readable:
                    try:
                        data = source.recv(65536)
                        if not data:
                            peers[source].shutdown(socket.SHUT_WR)
                            del peers[source]
                        else:
                            peers[source].sendall(data)
                    except (BrokenPipeError, ConnectionResetError):
                        return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-socket", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    servers = [Relay(9151, args.model_socket)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            parser.error("command required")
        return subprocess.call(command)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
