import json
import mimetypes
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from base_plugin import (
    BasePlugin,
    HookResult,
    MenuItemData,
    MenuItemType,
)
from client_utils import (
    get_file_loader,
    get_last_fragment,
    get_messages_controller,
    get_selected_account,
    send_request,
)
from file_utils import ensure_dir_exists, get_plugins_dir, read_file, write_file
from ui.bulletin import BulletinHelper
from ui.settings import Divider, Header, Input, Switch, Text

__id__ = "chat_api_bridge"
__name__ = "Chat API Bridge"
__description__ = "Exposes a selected chat over a local REST API on 127.0.0.1:16660"
__author__ = "local"
__version__ = "1.0.0"
__icon__ = "exteraPlugins/1"
__app_version__ = ">=12.5.1"
__sdk_version__ = ">=1.4.5.0"

DEFAULT_PORT = 16660
MAX_CACHE = 3000
FLUSH_INTERVAL = 5.0
SEND_HOOKS = ["TL_messages_sendMessage", "TL_messages_sendMedia", "TL_messages_sendMultiMedia"]


def peer_dialog_id(peer):
    if peer is None:
        return 0
    try:
        name = str(peer.getClass().getSimpleName())
    except Exception:
        return 0
    try:
        if "PeerUser" in name:
            return int(peer.user_id)
        if "PeerChannel" in name:
            return -(1000000000000 + int(peer.channel_id))
        if "PeerChat" in name:
            return -int(peer.chat_id)
    except Exception:
        return 0
    return 0


def make_api_handler(plugin):
    class ApiHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _json(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            token = plugin.get_setting("token", "")
            if not token:
                return True
            header = self.headers.get("Authorization", "")
            supplied = header[7:] if header.lower().startswith("bearer ") else ""
            if not supplied:
                supplied = self.headers.get("X-Token", "")
            return supplied == token

        def _read_body(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return None

        def _binary(self, path, mime, filename):
            size = os.path.getsize(path)
            guessed = mime or mimetypes.guess_type(filename)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", guessed)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", 'inline; filename="%s"' % filename.replace('"', ""))
            self.end_headers()
            with open(path, "rb") as f:
                bytes_done = 0
                while bytes_done < size:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    bytes_done += len(chunk)

        def _match_dynamic_get(self, path, query):
            parts = [p for p in path.split("/") if p]
            if len(parts) == 2 and parts[0] == "messages":
                query["_id"] = parts[1]
                return self._message_by_id, query
            if len(parts) in (2, 3) and parts[0] == "media":
                query["_id"] = parts[1]
                if len(parts) == 3 and parts[2] == "info":
                    return self._media_info, query
                if len(parts) == 3 and parts[2] == "size":
                    return self._media_size, query
                if len(parts) == 2:
                    return self._media_get, query
            return None, query

        def _match_dynamic_post(self, path, query):
            parts = [p for p in path.split("/") if p]
            if len(parts) == 3 and parts[0] == "media" and parts[2] == "download":
                query["_id"] = parts[1]
                return self._media_download, query
            return None, query

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method):
            try:
                if not self._authorized():
                    self._json(401, {"error": "unauthorized"})
                    return
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path.rstrip("/")
                query = urllib.parse.parse_qs(parsed.query)
                routes = {
                    "GET": {
                        "": self._index,
                        "/health": self._health,
                        "/chat": self._chat,
                        "/messages": self._messages,
                        "/search": self._search,
                        "/dialogs": self._dialogs,
                    },
                    "POST": {
                        "/send": self._send,
                        "/refresh": self._refresh,
                    },
                }
                handler = routes.get(method, {}).get(path)
                if handler is None and method == "GET":
                    handler, query = self._match_dynamic_get(path, query)
                if handler is None and method == "POST":
                    handler, query = self._match_dynamic_post(path, query)
                if handler is None:
                    self._json(404, {"error": "not found"})
                    return
                result = handler(query)
                if isinstance(result, tuple) and len(result) == 2:
                    self._json(result[0], result[1])
                elif result is not None:
                    self._json(200, result)
            except Exception as e:
                plugin.logger.exception("api error: %s", e)
                try:
                    self._json(500, {"error": str(e)})
                except Exception:
                    pass

        def _index(self, q):
            return {
                "endpoints": [
                    "GET /health",
                    "GET /chat",
                    "GET /messages?limit=100&before_id=<id>&after_id=<id>",
                    "GET /messages/<id>",
                    "GET /search?q=<text>&limit=50",
                    "GET /dialogs",
                    "GET /media/<id>            file if downloaded, else 409 not_downloaded",
                    "GET /media/<id>/info       media metadata and download state",
                    "GET /media/<id>/size       size of a downloaded file in bytes",
                    "POST /media/<id>/download  start media download, then poll GET /media/<id>",
                    "POST /send {\"text\": \"...\", \"reply_to\": <id>}",
                    "POST /refresh",
                ]
            }

        def _health(self, q):
            return {
                "status": "ok",
                "plugin": "chat_api_bridge",
                "enabled": bool(plugin.get_setting("enabled", True)),
                "target_dialog": plugin.target_dialog_id(),
                "cached_messages": plugin.cache_size(),
            }

        def _chat(self, q):
            dialog_id = plugin.target_dialog_id()
            if not dialog_id:
                return {"error": "no target dialog selected, pick one via plugin settings or the message context menu"}
            info = {
                "dialog_id": dialog_id,
                "cached_messages": plugin.cache_size(),
                "oldest_id": plugin.oldest_id(),
                "newest_id": plugin.newest_id(),
            }
            title = plugin.lookup_title(dialog_id)
            if title:
                info["title"] = title
            return info

        def _messages(self, q):
            if not plugin.target_dialog_id():
                return {"error": "no target dialog selected"}
            def param(name, default, cast):
                try:
                    return cast(q.get(name, [default])[0])
                except Exception:
                    return default
            limit = param("limit", 100, int)
            limit = max(1, min(limit, 1000))
            before_id = param("before_id", 0, int)
            after_id = param("after_id", 0, int)
            order = param("order", "desc", str)
            items = plugin.query_messages(limit, before_id, after_id, order)
            return {"count": len(items), "messages": items}

        def _message_by_id(self, q):
            try:
                message_id = int(q["_id"])
            except Exception:
                return (400, {"error": "invalid message id"})
            entry = plugin.get_message_by_id(message_id)
            if entry is None:
                return (404, {"error": "message not found in cache"})
            return {"message": entry}

        def _parse_media_id(self, q):
            try:
                return int(q.get("_id"))
            except Exception:
                return None

        def _media_get(self, q):
            message_id = self._parse_media_id(q)
            if message_id is None:
                return (400, {"error": "invalid message id"})
            state = plugin.media_state(message_id)
            if "error" in state:
                return (404, state)
            status = state.get("status")
            if status == "downloaded":
                self._binary(state["local_path"], state.get("mime"), state.get("filename"))
                return None
            if status == "not_downloaded":
                return (409, state)
            if status == "unsupported_media":
                return (415, state)
            if status == "no_media":
                return (404, state)
            return (500, state)

        def _media_info(self, q):
            message_id = self._parse_media_id(q)
            if message_id is None:
                return (400, {"error": "invalid message id"})
            state = plugin.media_state(message_id, allow_missing=True)
            if "error" in state:
                return (404, state)
            return state

        def _media_size(self, q):
            message_id = self._parse_media_id(q)
            if message_id is None:
                return (400, {"error": "invalid message id"})
            state = plugin.media_state(message_id)
            if "error" in state:
                return (404, state)
            if state.get("status") == "downloaded":
                return {
                    "message_id": message_id,
                    "size": os.path.getsize(state["local_path"]),
                    "filename": state.get("filename"),
                }
            if state.get("status") == "not_downloaded":
                return (409, state)
            return (415, state)

        def _media_download(self, q):
            message_id = self._parse_media_id(q)
            if message_id is None:
                return (400, {"error": "invalid message id"})
            state = plugin.media_state(message_id, allow_missing=True)
            if "error" in state:
                return (404, state)
            if state.get("status") == "downloaded":
                return (
                    200,
                    {
                        "status": "already_downloaded",
                        "message_id": message_id,
                        "local_path": state.get("local_path"),
                        "fetch": "GET /media/<id>",
                    },
                )
            if not state.get("raw_available"):
                return (
                    409,
                    {
                        "error": "message raw object is not in memory; open the target chat or POST /refresh",
                        "message_id": message_id,
                    },
                )
            if state.get("status") == "unsupported_media":
                return (415, {"error": "this media kind cannot be transferred as a file", "message_id": message_id})
            if state.get("status") == "no_media":
                return (404, {"error": "message has no media", "message_id": message_id})
            queued = plugin.start_media_download(message_id)
            if not queued:
                return (500, {"error": "failed to start download", "message_id": message_id})
            return (
                202,
                {
                    "status": "downloading",
                    "message_id": message_id,
                    "poll": "GET /media/<id>",
                },
            )

        def _search(self, q):
            if not plugin.target_dialog_id():
                return {"error": "no target dialog selected"}
            try:
                text = q.get("q", [""])[0]
            except Exception:
                text = ""
            try:
                limit = max(1, min(int(q.get("limit", ["50"])[0]), 500))
            except Exception:
                limit = 50
            return {"count": 0, "messages": plugin.search_messages(text, limit)}

        def _dialogs(self, q):
            return {"dialogs": plugin.list_dialogs()}

        def _send(self, q):
            if not plugin.get_setting("allow_send", False):
                return {"error": "sending is disabled in plugin settings (allow_send)"}
            body = self._read_body()
            if body is None:
                return {"error": "invalid JSON body"}
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                return {"error": "text must be a non-empty string"}
            reply_to = body.get("reply_to")
            if isinstance(reply_to, str):
                try:
                    reply_to = int(reply_to)
                except Exception:
                    reply_to = None
            if reply_to is not None and not isinstance(reply_to, int):
                reply_to = None
            dialog_id = plugin.target_dialog_id()
            if not dialog_id:
                return {"error": "no target dialog selected"}
            account = get_selected_account()
            client = get_client(account)
            plugin.run_on_queue_work(
                lambda: client.send_text(dialog_id, text, replyToMsg=reply_to)
            )
            return {"queued": True, "dialog_id": dialog_id, "text": text}

        def _refresh(self, q):
            dialog_id = plugin.target_dialog_id()
            if not dialog_id:
                return {"error": "no target dialog selected"}
            account = get_selected_account()
            queued = plugin.trigger_history_refresh(dialog_id, account)
            return {"queued": queued, "dialog_id": dialog_id, "account": account}

    return ApiHandler


class ChatApiBridgePlugin(BasePlugin):
    def on_plugin_load(self):
        self._lock = threading.Lock()
        self._messages = {}
        self._raw_messages = {}
        self._active_downloads = set()
        self._pending_flush = False
        self._last_flush = 0.0
        self._last_requests = {}
        self._server = None
        self._server_thread = None
        self._load_cache()
        self._maybe_start_server()
        self.add_hook("TL_messages_getHistory")
        self.add_hook("TL_updateNewMessage")
        for hook_name in SEND_HOOKS:
            self.add_hook(hook_name)
        self.menu_item_id = self.add_menu_item(
            MenuItemData(
                menu_type=MenuItemType.MESSAGE_CONTEXT_MENU,
                text="Expose via Chat API",
                on_click=self._on_menu_click,
                icon="msg_info",
                subtext="Target this chat for the local REST API",
                priority=5,
            )
        )
        self.logger.info("Chat API Bridge loaded")

    def on_plugin_unload(self):
        self._stop_server()
        self._flush_cache(force=True)
        self.logger.info("Chat API Bridge unloaded")

    def create_settings(self):
        return [
            Header(text="Chat API Bridge"),
            Switch(
                key="enabled",
                text="Run local API on 127.0.0.1:16660",
                default=True,
                subtext="Auto-refresh on START, announces via bulletin",
                icon="msg_settings",
                on_change=self._on_enabled_change,
            ),
            Input(
                key="dialog_id",
                text="Target dialog ID",
                default="",
                subtext="User: 123456789 | chat: -987654321 | channel: -1001234567890",
                icon="msg_list",
            ),
            Input(
                key="token",
                text="API token",
                default="",
                subtext="Optional. Send it as 'Authorization: Bearer <token>' or 'X-Token' header",
                icon="msg_locked",
            ),
            Switch(
                key="allow_send",
                text="Allow POST /send (real sends)",
                default=False,
                subtext="When enabled, the API can send messages into the target chat",
                icon="msg_edit",
                on_change=self._on_allow_send_change,
            ),
            Divider(),
            Text(
                text="Pick current chat",
                icon="msg_arrow_forward",
                subtext="Uses the chat currently open on screen",
                on_click=self._pick_current_chat,
            ),
            Text(
                text="API endpoints",
                icon="msg_info",
                subtext="GET /health /chat /messages /media /search /dialogs; POST /send /refresh",
            ),
        ]

    def _on_enabled_change(self, new_value):
        if new_value:
            self._maybe_start_server()
        else:
            self._stop_server()

    def _on_allow_send_change(self, new_value):
        self.logger.info("allow_send set to %s", new_value)

    def _on_menu_click(self, context):
        dialog_id = context.get("dialog_id")
        if not dialog_id:
            BulletinHelper.show_error("No dialog id in menu context")
            return
        dialog_id = int(dialog_id)
        self.set_setting("dialog_id", str(dialog_id), reload_settings=True)
        BulletinHelper.show_info("Chat API target set to %s" % dialog_id)

    def _pick_current_chat(self, view):
        fragment = get_last_fragment()
        dialog_id = None
        if fragment is not None:
            for method_name in ("getDialogId", "getParentDialogId", "getChatId"):
                try:
                    method = getattr(fragment, method_name, None)
                    if callable(method):
                        value = method()
                        if value:
                            dialog_id = int(value)
                            break
                except Exception:
                    continue
        if not dialog_id:
            BulletinHelper.show_error("Open a chat screen first")
            return
        self.set_setting("dialog_id", str(dialog_id), reload_settings=True)
        BulletinHelper.show_info("Chat API target set to %s" % dialog_id)

    def target_dialog_id(self):
        try:
            return int(self.get_setting("dialog_id", "") or 0)
        except Exception:
            return 0

    def _maybe_start_server(self):
        if not self.get_setting("enabled", True):
            return
        if self._server is not None:
            return
        try:
            handler = make_api_handler(self)
            server = ThreadingHTTPServer(("127.0.0.1", DEFAULT_PORT), handler)
            server.daemon_threads = True
            server.timeout = 0.5
            self._server = server
            self._server_thread = threading.Thread(
                target=self._serve_forever, name="chat_api_bridge_http", daemon=True
            )
            self._server_thread.start()
            self.logger.info("HTTP server listening on 127.0.0.1:%s", DEFAULT_PORT)
        except Exception as e:
            self._server = None
            self._server_thread = None
            self.logger.exception("Failed to start HTTP server: %s", e)

    def _serve_forever(self):
        self._server.serve_forever(poll_interval=0.5)

    def _stop_server(self):
        server = self._server
        thread = self._server_thread
        self._server = None
        self._server_thread = None
        if server is None:
            return
        try:
            server.shutdown()
        except Exception:
            pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
        try:
            server.server_close()
        except Exception:
            pass
        self.logger.info("HTTP server stopped")

    def cache_size(self):
        with self._lock:
            return len(self._messages)

    def oldest_id(self):
        with self._lock:
            if not self._messages:
                return None
            return min(self._messages.keys())

    def newest_id(self):
        with self._lock:
            if not self._messages:
                return None
            return max(self._messages.keys())

    def get_message_by_id(self, message_id):
        with self._lock:
            entry = self._messages.get(message_id)
            return dict(entry) if entry else None

    def query_messages(self, limit, before_id=0, after_id=0, order="desc"):
        with self._lock:
            keys = sorted(self._messages.keys(), reverse=True)
            if before_id:
                keys = [k for k in keys if k < before_id]
            if after_id:
                keys = [k for k in keys if k > after_id]
            if order == "asc":
                keys = list(reversed(keys))[:limit]
                keys = sorted(keys)
            else:
                keys = keys[:limit]
            return [dict(self._messages[k]) for k in keys]

    def search_messages(self, text, limit):
        needle = text.strip().lower()
        if not needle:
            return []
        with self._lock:
            keys = sorted(self._messages.keys(), reverse=True)
            hits = []
            for key in keys:
                entry = self._messages[key]
                haystack = entry.get("text") or ""
                if needle in haystack.lower():
                    hits.append(dict(entry))
                    if len(hits) >= limit:
                        break
            return hits

    def lookup_title(self, dialog_id):
        account = get_selected_account()
        controller = get_messages_controller(account)
        if controller is None:
            return None
        try:
            if dialog_id > 0:
                user = controller.getUser(dialog_id)
                if user is not None:
                    parts = [user.first_name, user.last_name]
                    return " ".join(p for p in parts if p) or None
            elif dialog_id < -1000000000000:
                chat = controller.getChannel(-dialog_id - 1000000000000)
                if chat is not None:
                    return chat.title or None
            else:
                chat = controller.getChat(-dialog_id)
                if chat is not None:
                    return chat.title or None
        except Exception:
            self.logger.debug("lookup_title failed for %s", dialog_id)
        return None

    def list_dialogs(self):
        account = get_selected_account()
        controller = get_messages_controller(account)
        if controller is None:
            return []
        dialogs = getattr(controller, "dialogs", None)
        if not dialogs:
            return []
        result = []
        for dialog in dialogs:
            try:
                dialog_id = int(dialog.id)
                entry = {
                    "dialog_id": dialog_id,
                    "type": "user" if dialog_id > 0 else (
                        "channel" if dialog_id < -1000000000000 else "chat"
                    ),
                }
                title = self.lookup_title(dialog_id)
                if title:
                    entry["title"] = title
                result.append(entry)
            except Exception:
                continue
        return result

    def trigger_history_refresh(self, dialog_id, account):
        peer = self._resolve_peer(dialog_id, account)
        if peer is None:
            return False
        request = TLRPC.TL_messages_getHistory()
        request.peer = peer
        request.limit = 100
        request.offset_id = 0
        request.add_offset = 0
        request.offset_date = 0
        request.max_id = 0
        request.min_id = 0
        request.hash = 0
        send_request(
            request,
            lambda response, error: self._on_history_response(dialog_id, response, error),
            account=account,
        )
        return True

    def _resolve_peer(self, dialog_id, account):
        controller = get_messages_controller(account)
        if controller is None:
            return None
        try:
            if dialog_id > 0:
                user = controller.getUser(dialog_id)
                peer = TLRPC.TL_inputPeerUser()
                peer.user_id = dialog_id
                peer.access_hash = user.access_hash if user is not None else 0
                return peer
            if dialog_id < -1000000000000:
                channel_id = -dialog_id - 1000000000000
                chat = controller.getChannel(channel_id)
                peer = TLRPC.TL_inputPeerChannel()
                peer.channel_id = channel_id
                peer.access_hash = chat.access_hash if chat is not None else 0
                return peer
            peer = TLRPC.TL_inputPeerChat()
            peer.chat_id = -dialog_id
            return peer
        except Exception:
            self.logger.exception("resolve peer failed for %s", dialog_id)
            return None

    def run_on_queue_work(self, target):
        from client_utils import run_on_queue

        run_on_queue(target)

    def pre_request_hook(self, request_name, account, request):
        self._last_requests[account] = (request_name, request)
        return HookResult()

    def post_request_hook(self, request_name, account, response, error):
        stash = self._last_requests.pop(account, None)
        if error:
            return HookResult()
        if request_name == "TL_messages_getHistory":
            self._on_history_response_from_stash(stash, response, account)
        elif request_name in SEND_HOOKS:
            self._on_send_response(stash, response)
        return HookResult()

    def _on_history_response_from_stash(self, stash, response, account):
        dialog_id = 0
        if stash and stash[0] == "TL_messages_getHistory":
            dialog_id = peer_dialog_id(stash[1].peer)
        self._on_history_response_any(dialog_id, response)

    def _on_history_response(self, dialog_id, response, error):
        if error:
            self.logger.warning("history refresh failed: %s", error)
            return
        self._on_history_response_any(dialog_id, response)

    def _on_history_response_any(self, dialog_id, response):
        if response is None:
            return
        try:
            if not isinstance(response, TLRPC.TL_messages_Messages):
                return
        except Exception:
            return
        try:
            additions = []
            raws = []
            for message in response.messages:
                entry = self._message_entry(message, dialog_id)
                if entry:
                    additions.append(entry)
                    raws.append(message)
            if additions:
                self._add_entries(additions, raws)
                self.logger.debug("cached %s messages from history in %s", len(additions), dialog_id)
        except Exception:
            self.logger.exception("failed to process history response")

    def _on_send_response(self, stash, response):
        dialog_id = 0
        text = ""
        if stash:
            try:
                dialog_id = peer_dialog_id(stash[1].peer)
                text = stash[1].message
            except Exception:
                pass
        if not dialog_id:
            return
        additions = []
        raws = []
        try:
            if isinstance(response, TLRPC.TL_updates):
                message_id = 0
                message = None
                for update in response.updates:
                    if isinstance(update, TLRPC.TL_updateNewMessage):
                        message = update.message
                    elif isinstance(update, TLRPC.TL_updateMessageID):
                        message_id = int(update.id)
                if message is not None:
                    entry = self._message_entry(message, dialog_id)
                    if entry:
                        additions.append(entry)
                        raws.append(message)
                elif message_id and text:
                    additions.append(
                        {
                            "id": message_id,
                            "dialog_id": dialog_id,
                            "date": int(time.time()),
                            "text": text,
                            "media": None,
                            "action": None,
                            "from_id": None,
                            "out": True,
                            "reply_to": None,
                            "captured_at": int(time.time()),
                        }
                    )
                    raws.append(None)
            elif isinstance(response, TLRPC.TL_updateShortMessage):
                additions.append(
                    {
                        "id": int(response.id),
                        "dialog_id": int(response.from_id),
                        "date": int(response.date),
                        "text": response.message,
                        "media": None,
                        "action": None,
                        "from_id": int(response.from_id) if response.out else None,
                        "out": bool(response.out),
                        "reply_to": int(response.reply_to_msg_id) if response.reply_to_msg_id else None,
                        "captured_at": int(time.time()),
                    }
                )
            elif isinstance(response, TLRPC.TL_updateShortChatMessage):
                additions.append(
                    {
                        "id": int(response.id),
                        "dialog_id": -int(response.chat_id),
                        "date": int(response.date),
                        "text": response.message,
                        "media": None,
                        "action": None,
                        "from_id": int(response.from_id),
                        "out": bool(response.out),
                        "reply_to": None,
                        "captured_at": int(time.time()),
                    }
                )
            elif isinstance(response, TLRPC.TL_updateShortSentMessage):
                if text:
                    additions.append(
                        {
                            "id": int(response.id),
                            "dialog_id": dialog_id,
                            "date": int(response.date),
                            "text": text,
                            "media": None,
                            "action": None,
                            "from_id": None,
                            "out": True,
                            "reply_to": None,
                            "captured_at": int(time.time()),
                        }
                    )
        except Exception:
            self.logger.exception("failed to process send response")
        if additions:
            self._add_entries(additions, raws)

    def on_update_hook(self, update_name, account, update):
        if update_name == "TL_updateNewMessage":
            try:
                message = update.message
                target = self.target_dialog_id()
                if message is not None and target:
                    dialog_id = int(getattr(message, "dialog_id", 0) or 0)
                    if dialog_id == target:
                        entry = self._message_entry(message, dialog_id)
                        if entry:
                            self._add_entries([entry], [message])
                            self.logger.debug("cached new message %s in %s", entry["id"], dialog_id)
            except Exception:
                self.logger.exception("failed to cache updateNewMessage")
        return HookResult()

    def _message_entry(self, message, dialog_id=None):
        try:
            resolved = dialog_id
            if not resolved:
                resolved = int(getattr(message, "dialog_id", 0) or 0)
            if not resolved:
                return None
            text = getattr(message, "message", None)
            if not isinstance(text, str):
                text = None
            media_type = None
            try:
                if getattr(message, "media", None) is not None:
                    media_type = str(message.media.getClass().getSimpleName())
            except Exception:
                pass
            action_type = None
            try:
                if getattr(message, "action", None) is not None:
                    action_type = str(message.action.getClass().getSimpleName())
            except Exception:
                pass
            from_id = None
            try:
                from_peer = getattr(message, "from_id", None)
                if from_peer is not None and getattr(from_peer, "user_id", 0):
                    from_id = int(from_peer.user_id)
            except Exception:
                pass
            reply_to = None
            try:
                reply = getattr(message, "reply_to", None)
                if reply is not None and getattr(reply, "reply_to_msg_id", 0):
                    reply_to = int(reply.reply_to_msg_id)
            except Exception:
                pass
            message_id = int(message.id)
            if message_id <= 0:
                return None
            return {
                "id": message_id,
                "dialog_id": int(resolved),
                "date": int(message.date),
                "text": text,
                "media": media_type,
                "action": action_type,
                "from_id": from_id,
                "out": bool(message.out),
                "reply_to": reply_to,
                "captured_at": int(time.time()),
            }
        except Exception:
            self.logger.debug("failed to build message entry")
            return None

    def _add_entries(self, entries, raws=None):
        target = self.target_dialog_id()
        with self._lock:
            changed = False
            for index, entry in enumerate(entries):
                if target and entry.get("dialog_id") != target:
                    continue
                self._messages[entry["id"]] = entry
                changed = True
                if raws is not None and index < len(raws) and raws[index] is not None:
                    self._raw_messages[entry["id"]] = raws[index]
            if changed and len(self._messages) > MAX_CACHE:
                for old_id in sorted(self._messages.keys())[: len(self._messages) - MAX_CACHE]:
                    del self._messages[old_id]
                    self._raw_messages.pop(old_id, None)
        if changed:
            maybe = self._maybe_flush()
            if maybe:
                self._flush_cache()

    def _maybe_flush(self):
        now = time.time()
        with self._lock:
            if now - self._last_flush >= FLUSH_INTERVAL:
                self._last_flush = now
                return True
            self._pending_flush = True
            return False

    def media_state(self, message_id, allow_missing=False):
        target = self.target_dialog_id()
        with self._lock:
            entry = self._messages.get(message_id)
            raw = self._raw_messages.get(message_id)
            active = message_id in self._active_downloads
        if entry is None:
            return {"error": "message not found in cache", "message_id": message_id}
        if target and entry.get("dialog_id") != target:
            return {"error": "message belongs to another dialog", "message_id": message_id}
        media_type = entry.get("media")
        base = {"message_id": message_id, "media_type": media_type}
        if not media_type:
            base["status"] = "no_media"
            return base
        if raw is None:
            base["status"] = "not_downloaded"
            base["raw_available"] = False
            base["downloading"] = active
            base["hint"] = (
                "message raw object is not in memory; open the target chat or POST /refresh, "
                "then POST /media/<id>/download"
            )
            return base
        state = self._media_state_for_raw(raw, message_id, media_type)
        state["message_id"] = message_id
        state["media_type"] = media_type
        state["raw_available"] = True
        state["downloading"] = active
        if state.get("status") == "downloaded":
            with self._lock:
                self._active_downloads.discard(message_id)
        if allow_missing and not state.get("hint") and state.get("status") == "not_downloaded":
            state["hint"] = "POST /media/<id>/download then poll GET /media/<id>"
        return state

    def _media_state_for_raw(self, raw, message_id, media_type):
        try:
            media = getattr(raw, "media", None)
            if media is None:
                return {"status": "no_media"}
            document = getattr(media, "document", None)
            if document is not None:
                state = {"kind": "document"}
                state["filename"] = self.document_filename(document, message_id)
                state["mime"] = getattr(document, "mime_type", None) or "application/octet-stream"
                try:
                    state["bytes_hint"] = int(document.size)
                except Exception:
                    state["bytes_hint"] = None
                path = self._local_media_path_obj(document)
                if path:
                    state["status"] = "downloaded"
                    state["local_path"] = path
                else:
                    state["status"] = "not_downloaded"
                    state["download_object"] = document
                return state
            photo = getattr(media, "photo", None)
            sizes = self.photo_sizes_desc(photo)
            if photo is not None and sizes:
                state = {"kind": "photo", "filename": "photo_%s.jpg" % message_id, "mime": "image/jpeg"}
                for size in sizes:
                    path = self._local_media_path_obj(size)
                    if path:
                        state["status"] = "downloaded"
                        state["local_path"] = path
                        return state
                state["status"] = "not_downloaded"
                state["download_object"] = sizes[0]
                return state
            return {"status": "unsupported_media"}
        except Exception:
            self.logger.exception("failed to resolve media for message %s", message_id)
            return {"status": "resolve_error"}

    def photo_sizes_desc(self, photo):
        sizes = []
        try:
            for size in photo.sizes:
                try:
                    cls = str(size.getClass().getSimpleName())
                except Exception:
                    cls = ""
                if "Stripped" in cls or "Empty" in cls:
                    continue
                w = int(getattr(size, "w", 0) or 0)
                h = int(getattr(size, "h", 0) or 0)
                byte_size = int(getattr(size, "size", 0) or 0)
                sizes.append((w * h + byte_size, size))
        except Exception:
            return []
        return [size for score, size in sorted(sizes, key=lambda pair: pair[0], reverse=True)]

    def document_filename(self, document, message_id):
        for field in ("file_name", "name", "title"):
            value = getattr(document, field, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        mime = getattr(document, "mime_type", None)
        ext = "bin"
        if isinstance(mime, str) and "/" in mime:
            guess = mimetypes.guess_extension(mime.split(";")[0].strip())
            if guess:
                ext = guess.lstrip(".")
        return "file_%s.%s" % (message_id, ext)

    def _local_media_path_obj(self, obj):
        try:
            loader = get_file_loader(get_selected_account())
        except Exception:
            loader = None
        if loader is None:
            return None
        try:
            candidate = loader.getPathToAttach(obj)
        except Exception:
            candidate = None
        path = self._as_absolute_path(candidate)
        if path and os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
        return None

    def _as_absolute_path(self, candidate):
        if candidate is None:
            return None
        try:
            get_absolute_path = getattr(candidate, "getAbsolutePath", None)
            if callable(get_absolute_path):
                value = get_absolute_path()
            else:
                value = candidate
        except Exception:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None

    def start_media_download(self, message_id):
        with self._lock:
            if message_id in self._active_downloads:
                return True
            raw = self._raw_messages.get(message_id)
            if raw is None or not self._messages.get(message_id):
                return False
            self._active_downloads.add(message_id)
        self.run_on_queue_work(lambda: self._do_media_download(message_id, raw))
        return True

    def _do_media_download(self, message_id, raw):
        try:
            media = getattr(raw, "media", None)
            if media is None:
                return
            loader = get_file_loader(get_selected_account())
            if loader is None:
                self.logger.warning("FileLoader instance unavailable for media download")
                return
            attempts = []
            document = getattr(media, "document", None)
            if document is not None:
                attempts = self.document_download_attempts(loader, document, media)
            else:
                photo = getattr(media, "photo", None)
                sizes = self.photo_sizes_desc(photo)
                if photo is not None and sizes:
                    attempts = self.photo_download_attempts(loader, sizes[0], media)
            started = False
            for attempt in attempts:
                try:
                    attempt()
                    started = True
                    self.logger.debug("media download triggered for message %s", message_id)
                    break
                except Exception as e:
                    self.logger.debug("download attempt failed for message %s: %s", message_id, e)
            if not started:
                self.logger.warning("could not start download for message %s", message_id)
        finally:
            with self._lock:
                self._active_downloads.discard(message_id)

    def document_download_attempts(self, loader, document, media):
        size = None
        try:
            size = int(document.size)
        except Exception:
            size = 0
        return [
            lambda: loader.loadFile(document, size, 1),
            lambda: loader.loadFile(document, size, 2),
            lambda: loader.loadFile(document, 1),
            lambda: loader.loadFile(document, 0, 1),
            lambda: loader.loadFile(media, size, 1),
        ]

    def photo_download_attempts(self, loader, size, media):
        return [
            lambda: loader.loadFile(size, media, 1),
            lambda: loader.loadFile(size, media, 2),
            lambda: loader.loadFile(size, "chat_api_bridge", "jpg", False),
            lambda: loader.loadFile(size, 1),
        ]

    def _cache_path(self):
        data_dir = os.path.join(get_plugins_dir(), "chat_api_bridge_data")
        ensure_dir_exists(data_dir)
        return os.path.join(data_dir, "cache.json")

    def _load_cache(self):
        try:
            raw = read_file(self._cache_path())
            if not raw:
                return
            data = json.loads(raw)
            target = self.target_dialog_id()
            with self._lock:
                if isinstance(data, list):
                    for entry in data:
                        try:
                            entry["id"] = int(entry["id"])
                            entry["dialog_id"] = int(entry["dialog_id"])
                            if target and entry["dialog_id"] != target:
                                continue
                            self._messages[entry["id"]] = entry
                        except Exception:
                            continue
        except Exception:
            self.logger.debug("failed to load cache")

    def _flush_cache(self, force=False):
        if not force:
            with self._lock:
                if self._last_flush <= 0:
                    self._last_flush = time.time()
                    self._pending_flush = False
                else:
                    self._pending_flush = False
        snapshot = None
        with self._lock:
            snapshot = [dict(self._messages[k]) for k in sorted(self._messages.keys())]
        try:
            write_file(
                self._cache_path(),
                json.dumps(snapshot, ensure_ascii=False),
            )
        except Exception:
            self.logger.exception("failed to write cache file")

    def on_app_event(self, event_type):
        try:
            from base_plugin import AppEvent

            if event_type == AppEvent.START:
                self._maybe_start_server()
            elif event_type == AppEvent.STOP:
                self._flush_cache(force=True)
        except Exception:
            pass

