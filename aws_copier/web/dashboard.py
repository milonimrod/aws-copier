"""Live web dashboard for AWS Copier — streams logs to browser via SSE."""

import asyncio
import json
import logging
import threading
from typing import TYPE_CHECKING, List, Optional

from aiohttp import web

if TYPE_CHECKING:
    from aws_copier.core.file_listener import FileListener

logger = logging.getLogger(__name__)

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AWS Copier Dashboard</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           background: #0f1117; color: #e2e8f0; height: 100vh; display: flex;
           flex-direction: column; }
    header { background: #1a1f2e; border-bottom: 1px solid #2d3748; padding: 12px 20px;
             display: flex; align-items: center; gap: 20px; flex-wrap: wrap; }
    h1 { font-size: 1.1rem; font-weight: 600; color: #63b3ed; white-space: nowrap; }
    #stats { display: flex; gap: 16px; flex-wrap: wrap; flex: 1; }
    .stat { display: flex; flex-direction: column; align-items: center; }
    .stat-val { font-size: 1.4rem; font-weight: 700; color: #68d391; }
    .stat-lbl { font-size: 0.65rem; color: #718096; text-transform: uppercase; letter-spacing: .05em; }
    .stat-val.err { color: #fc8181; }
    .stat-val.skip { color: #f6ad55; }
    .stat-val.scan { color: #76e4f7; }
    #controls { display: flex; align-items: center; gap: 12px; }
    label { font-size: 0.8rem; color: #a0aec0; cursor: pointer; display: flex;
            align-items: center; gap: 5px; }
    button { background: #2d3748; border: 1px solid #4a5568; color: #e2e8f0;
             padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 0.8rem; }
    button:hover { background: #4a5568; }
    #conn-status { width: 8px; height: 8px; border-radius: 50%; background: #fc8181;
                   display: inline-block; }
    #conn-status.live { background: #68d391; }
    #log-wrap { flex: 1; overflow-y: auto; padding: 8px 12px; }
    #log-output { font-family: 'Menlo', 'Consolas', 'Courier New', monospace;
                  font-size: 0.78rem; line-height: 1.55; }
    .log-line { padding: 1px 0; border-bottom: 1px solid #1a1f2e; white-space: pre-wrap;
                word-break: break-all; }
    .log-line.INFO    { color: #e2e8f0; }
    .log-line.DEBUG   { color: #718096; }
    .log-line.WARNING { color: #f6ad55; }
    .log-line.ERROR   { color: #fc8181; }
    .log-line.CRITICAL{ color: #ff4444; font-weight: bold; }
    #filter { display: flex; gap: 6px; align-items: center; }
    select { background: #2d3748; border: 1px solid #4a5568; color: #e2e8f0;
             padding: 3px 6px; border-radius: 4px; font-size: 0.8rem; }
  </style>
</head>
<body>
  <header>
    <h1>&#9729; AWS Copier</h1>
    <div id="stats">
      <div class="stat"><span class="stat-val scan" id="s-scan">-</span><span class="stat-lbl">Scanned</span></div>
      <div class="stat"><span class="stat-val" id="s-up">-</span><span class="stat-lbl">Uploaded</span></div>
      <div class="stat"><span class="stat-val skip" id="s-skip">-</span><span class="stat-lbl">Skipped</span></div>
      <div class="stat"><span class="stat-val err" id="s-err">-</span><span class="stat-lbl">Errors</span></div>
    </div>
    <div id="controls">
      <div id="filter">
        <label for="lvl-filter">Level</label>
        <select id="lvl-filter">
          <option value="">All</option>
          <option value="DEBUG">DEBUG+</option>
          <option value="INFO">INFO+</option>
          <option value="WARNING">WARNING+</option>
          <option value="ERROR">ERROR+</option>
        </select>
      </div>
      <label><input type="checkbox" id="autoscroll" checked> Auto-scroll</label>
      <button onclick="clearLogs()">Clear</button>
      <span id="conn-status" title="Disconnected"></span>
    </div>
  </header>
  <div id="log-wrap">
    <div id="log-output"></div>
  </div>
  <script>
    const LEVEL_ORDER = {DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3, CRITICAL: 4};
    let minLevel = '';
    let es = null;
    let lineCount = 0;
    const MAX_LINES = 5000;

    const output = document.getElementById('log-output');
    const wrap = document.getElementById('log-wrap');
    const connDot = document.getElementById('conn-status');
    const scrollCb = document.getElementById('autoscroll');

    function levelOrder(lvl) { return LEVEL_ORDER[lvl] ?? 1; }
    function shouldShow(lvl) {
      if (!minLevel) return true;
      return levelOrder(lvl) >= levelOrder(minLevel);
    }

    function appendLine(entry) {
      if (!shouldShow(entry.level)) return;
      const div = document.createElement('div');
      div.className = 'log-line ' + (entry.level || 'INFO');
      div.textContent = entry.msg;
      output.appendChild(div);
      lineCount++;
      if (lineCount > MAX_LINES) {
        output.removeChild(output.firstChild);
        lineCount--;
      }
      if (scrollCb.checked) wrap.scrollTop = wrap.scrollHeight;
    }

    function clearLogs() { output.innerHTML = ''; lineCount = 0; }

    document.getElementById('lvl-filter').addEventListener('change', function() {
      minLevel = this.value;
      // Hide/show existing lines
      Array.from(output.children).forEach(el => {
        const lvl = el.className.replace('log-line ', '').trim();
        el.style.display = shouldShow(lvl) ? '' : 'none';
      });
    });

    function connect() {
      if (es) { es.close(); }
      es = new EventSource('/logs');
      es.onopen = () => connDot.classList.add('live');
      es.onerror = () => {
        connDot.classList.remove('live');
        es.close();
        setTimeout(connect, 3000);
      };
      es.onmessage = (ev) => {
        try { appendLine(JSON.parse(ev.data)); } catch(e) {}
      };
    }

    async function refreshStats() {
      try {
        const r = await fetch('/status');
        if (!r.ok) return;
        const d = await r.json();
        document.getElementById('s-scan').textContent  = d.files_scanned  ?? d.scanned  ?? '-';
        document.getElementById('s-up').textContent    = d.files_uploaded ?? d.uploaded ?? '-';
        document.getElementById('s-skip').textContent  = d.files_skipped  ?? d.skipped  ?? '-';
        document.getElementById('s-err').textContent   = d.errors ?? '-';
      } catch(e) {}
    }

    connect();
    refreshStats();
    setInterval(refreshStats, 5000);
  </script>
</body>
</html>
"""

_LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}


class LogBroadcaster(logging.Handler):
    """Logging handler that fans log records out to all connected SSE clients."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        """Initialize broadcaster bound to the given event loop."""
        super().__init__()
        self._loop = loop
        self._clients: List[asyncio.Queue] = []
        self._lock = threading.Lock()

    def add_client(self, queue: "asyncio.Queue[dict]") -> None:
        """Register a new SSE client queue."""
        with self._lock:
            self._clients.append(queue)

    def remove_client(self, queue: "asyncio.Queue[dict]") -> None:
        """Deregister a disconnected SSE client queue."""
        with self._lock:
            try:
                self._clients.remove(queue)
            except ValueError:
                pass

    def _enqueue(self, q: "asyncio.Queue[dict]", entry: dict) -> None:
        """Ring-buffer enqueue: drop oldest if full. Must run on the event loop."""
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            q.put_nowait(entry)
        except asyncio.QueueFull:
            pass

    def emit(self, record: logging.LogRecord) -> None:
        """Broadcast a formatted log record to all registered clients.

        Args:
            record: The log record to broadcast.
        """
        try:
            msg = self.format(record)
            entry = {"msg": msg, "level": record.levelname}
            with self._lock:
                clients = list(self._clients)
            for q in clients:
                try:
                    self._loop.call_soon_threadsafe(self._enqueue, q, entry)
                except Exception:
                    pass
        except Exception:
            self.handleError(record)


class WebDashboard:
    """Async web dashboard that streams application logs to the browser via SSE."""

    def __init__(self, file_listener: "FileListener", port: int = 8765) -> None:
        """Initialize dashboard.

        Args:
            file_listener: FileListener instance to pull status stats from.
            port: TCP port to listen on (default 8765).
        """
        self._file_listener = file_listener
        self._port = port
        self._broadcaster: Optional[LogBroadcaster] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

    async def start(self) -> None:
        """Start the aiohttp web server and install the log broadcaster."""
        loop = asyncio.get_running_loop()
        self._broadcaster = LogBroadcaster(loop)
        self._broadcaster.setFormatter(logging.Formatter("%(asctime)s  %(name)-30s  %(levelname)-8s  %(message)s"))
        logging.getLogger().addHandler(self._broadcaster)

        app = web.Application()
        app.router.add_get("/", self._handle_root)
        app.router.add_get("/logs", self._handle_sse)
        app.router.add_get("/status", self._handle_status)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await self._site.start()
        logger.info(f"Web dashboard listening on http://localhost:{self._port}")

    async def stop(self) -> None:
        """Stop the web server and remove the log broadcaster."""
        if self._broadcaster is not None:
            logging.getLogger().removeHandler(self._broadcaster)
            self._broadcaster = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _handle_root(self, _request: web.Request) -> web.Response:
        return web.Response(text=_HTML_TEMPLATE, content_type="text/html")

    async def _handle_sse(self, request: web.Request) -> web.StreamResponse:
        """Stream log records to the client via Server-Sent Events."""
        response = web.StreamResponse()
        response.headers["Content-Type"] = "text/event-stream"
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Accel-Buffering"] = "no"
        await response.prepare(request)

        assert self._broadcaster is not None
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._broadcaster.add_client(queue)
        try:
            while True:
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=20.0)
                    payload = f"data: {json.dumps(entry)}\n\n"
                    await response.write(payload.encode())
                except asyncio.TimeoutError:
                    # SSE comment line — browser ignores it, keeps TCP alive
                    await response.write(b": heartbeat\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self._broadcaster.remove_client(queue)
        return response

    async def _handle_status(self, _request: web.Request) -> web.Response:
        """Return current backup statistics as JSON."""
        stats = self._file_listener.get_statistics()
        return web.json_response(stats)
