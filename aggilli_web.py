#!/usr/bin/env python3
"""Servidor local do Canvas Aggilli."""

import argparse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from pathlib import Path
import secrets
import shutil
import tempfile
from threading import Lock, Thread

from aggilli import execute_agent


def workflow(nodes, edges):
    by_id = {node["id"]: node for node in nodes}
    if not nodes or len(by_id) != len(nodes):
        raise ValueError("Adicione agentes com identificadores únicos.")
    parents = {key: [] for key in by_id}
    children = {key: [] for key in by_id}
    for edge in edges:
        source, target = edge["from"], edge["to"]
        if source not in by_id or target not in by_id:
            raise ValueError("Há conexões com agentes ausentes.")
        if source not in parents[target]:
            parents[target].append(source)
            children[source].append(target)
    remaining = {key: len(values) for key, values in parents.items()}
    ready = deque(key for key, count in remaining.items() if count == 0)
    order = []
    while ready:
        key = ready.popleft()
        order.append(key)
        for child in children[key]:
            remaining[child] -= 1
            if remaining[child] == 0:
                ready.append(child)
    if len(order) != len(nodes):
        raise ValueError("Remova as conexões circulares antes de executar.")
    return by_id, parents, order


def tail(path):
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 24000))
            return stream.read(24000).decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


class Runner:
    def __init__(self, executable, history):
        self.executable = executable
        self.history = history
        self.lock = Lock()
        self.run = None

    def start(self, payload):
        task = payload["task"].strip()
        project = Path(payload["project"]).expanduser().resolve()
        if not task or not project.is_dir():
            raise ValueError("Informe uma tarefa e a pasta existente do projeto.")
        nodes, parents, order = workflow(payload["nodes"], payload["edges"])
        if not self.executable:
            raise ValueError("CLI do Codex ausente. Inicie o servidor com --codex CAMINHO.")
        with self.lock:
            if self.run and self.run["status"] == "running":
                raise ValueError("Já há uma execução em andamento.")
            self.history.mkdir(parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix="fluxo-", dir=self.history))
            steps = {
                key: {"status": "queued", "report": directory / f"{index}.md",
                      "log": directory / f"{index}.log"}
                for index, key in enumerate(order, 1)
            }
            run = {"status": "running", "directory": directory, "steps": steps}
            (directory / "fluxo.json").write_text(
                json.dumps({**payload, "order": order}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.record_status(run)
            self.run = run
            Thread(target=self.execute, args=(run, nodes, parents, order, project, task),
                   daemon=True).start()

    def execute(self, run, nodes, parents, order, project, task):
        reports = {}
        steps = run["steps"]
        for key in order:
            step = steps[key]
            if any(parent not in reports for parent in parents[key]):
                with self.lock:
                    step["status"] = "skipped"
                    self.record_status(run)
                continue
            with self.lock:
                step["status"] = "running"
                self.record_status(run)
            node = nodes[key]
            sandbox = "workspace-write" if node.get("write") is True else "read-only"
            try:
                context = "\n\n".join(
                    f"Relatório de {nodes[parent]['name']}:\n{reports[parent]}"
                    for parent in parents[key]
                )
                success, response = execute_agent(
                    self.executable, project, node["name"], node["instructions"],
                    task, context, sandbox, step["report"], step["log"],
                )
            except Exception as error:
                with step["log"].open("a", encoding="utf-8") as log:
                    log.write(f"\n{error}\n")
                success = False
            if success:
                reports[key] = response
            with self.lock:
                step["status"] = "done" if success else "failed"
                self.record_status(run)
        with self.lock:
            run["status"] = "done" if len(reports) == len(order) else "failed"
            self.record_status(run)

    def record_status(self, run):
        directory = run["directory"]
        data = {"status": run["status"], "steps": {
            key: step["status"] for key, step in run["steps"].items()
        }}
        temporary = directory / "status.tmp"
        temporary.write_text(json.dumps(data), encoding="utf-8")
        temporary.replace(directory / "status.json")

    def archive(self, run_id=None):
        if run_id is None:
            directories = sorted(self.history.glob("fluxo-*"),
                                 key=lambda path: path.stat().st_mtime, reverse=True)
        elif re.fullmatch(r"fluxo-[a-z0-9_]+", run_id):
            directories = [self.history / run_id]
        else:
            raise ValueError("Identificador de execução inválido.")
        entries = []
        for directory in directories:
            if directory.is_symlink():
                continue
            try:
                metadata = json.loads((directory / "fluxo.json").read_text(encoding="utf-8"))
                status_file = directory / "status.json"
                state = json.loads(status_file.read_text(encoding="utf-8")) if status_file.exists() else {}
            except (OSError, ValueError):
                continue
            status = state.get("status", "unknown")
            if status == "running":
                with self.lock:
                    if not self.run or self.run["directory"] != directory:
                        status = "interrupted"
            entry = {"id": directory.name, "task": metadata["task"],
                     "project": metadata["project"], "status": status,
                     "created": (directory / "fluxo.json").stat().st_mtime}
            if run_id:
                names = {node["id"]: node["name"] for node in metadata["nodes"]}
                entry["agents"] = [
                    {"name": names[key], "status": state.get("steps", {}).get(key, "unknown"),
                     "log": tail(directory / f"{index}.log"),
                     "report": tail(directory / f"{index}.md")}
                    for index, key in enumerate(metadata["order"], 1)
                ]
                return entry
            entries.append(entry)
            if len(entries) == 50:
                break
        return entries if run_id is None else None

    def snapshot(self):
        with self.lock:
            if not self.run:
                return {"status": "idle", "steps": {}}
            steps = {key: step.copy() for key, step in self.run["steps"].items()}
            status = self.run["status"]
            directory = str(self.run["directory"])
        return {"status": status, "directory": directory, "steps": {
            key: {"status": step["status"], "log": tail(step["log"]),
                  "report": tail(step["report"])} for key, step in steps.items()
        }}


def main():
    parser = argparse.ArgumentParser(description="Aggilli Canvas local")
    parser.add_argument("--porta", type=int, default=8765)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--historico", type=Path, default=Path("aggilli-execucoes"))
    args = parser.parse_args()
    executable = shutil.which(args.codex)
    runner = Runner(str(Path(executable).absolute()) if executable else None, args.historico.resolve())
    token = secrets.token_hex(32)
    html = Path(__file__).with_name("aggilli.html").read_text(encoding="utf-8")
    html = html.replace('const apiToken = "";', f'const apiToken = "{token}";', 1)

    class Handler(BaseHTTPRequestHandler):
        def send(self, status, body, content_type="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def reply(self, status, value):
            self.send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"))

        def allowed_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def authorized(self):
            return self.allowed_host() and secrets.compare_digest(
                self.headers.get("X-Aggilli-Token", "").encode(), token.encode()
            )

        def do_GET(self):
            if not self.allowed_host():
                self.reply(403, {"error": "Acesse pelo endereço local impresso no terminal."})
            elif self.path in ("/", "/aggilli.html"):
                self.send(200, html.encode("utf-8"), "text/html")
            elif self.path == "/api/status" and self.authorized():
                self.reply(200, runner.snapshot())
            elif self.path == "/api/history" and self.authorized():
                self.reply(200, runner.archive())
            elif self.path.startswith("/api/history/") and self.authorized():
                try:
                    entry = runner.archive(self.path.removeprefix("/api/history/"))
                except ValueError:
                    entry = None
                self.reply(200, entry) if entry else self.reply(404, {"error": "Execução indisponível."})
            else:
                self.reply(404, {"error": "Rota indisponível."})

        def do_POST(self):
            if self.path != "/api/run" or not self.authorized():
                self.reply(403, {"error": "Solicitação não autorizada."})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 256000 or self.headers.get_content_type() != "application/json":
                    raise ValueError("Formato ou tamanho da solicitação inválido.")
                runner.start(json.loads(self.rfile.read(size)))
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                self.reply(400, {"error": str(error)})
            except OSError as error:
                self.reply(500, {"error": str(error)})
            else:
                self.reply(202, {"status": "running"})

    with ThreadingHTTPServer(("127.0.0.1", args.porta), Handler) as server:
        print(f"Aggilli aberto em http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
