# =============================================================================
# services/bot/evals/fake_telegram.py — Servidor Telegram falso (mínimo que usa aiogram)
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

import asyncio
import json
import time

from aiohttp import web

BOT_USER = {"id": 999, "is_bot": True, "first_name": "Fake", "username": "fake_bot"}


class FakeTelegram:
    def __init__(self, token):
        self.token = token
        self.calls = []
        self.messages = {}
        self._updates = []
        self._next_update = 1000
        self._next_msg = 1
        self._cond = asyncio.Condition()
        self.app = web.Application()
        self.app.router.add_route("*", "/bot{token}/{method}", self.handle)

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}"

    async def stop(self):
        await self.runner.cleanup()

    def calls_of(self, method):
        return [c for c in self.calls if c["method"] == method]

    async def push(self, **payload):
        async with self._cond:
            self._next_update += 1
            self._updates.append({"update_id": self._next_update, **payload})
            self._cond.notify_all()

    @staticmethod
    def user(uid):
        return {"id": uid, "is_bot": False, "first_name": "U"}

    async def push_text(self, user_id, text, chat_id=None, chat_type="private", edited=False):
        chat_id = user_id if chat_id is None else chat_id
        msg = {"message_id": self._alloc(), "date": int(time.time()), "chat": {"id": chat_id, "type": chat_type},
               "from": self.user(user_id), "text": text}
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        await self.push(**{"edited_message" if edited else "message": msg})

    async def push_callback(self, user_id, data, chat_id, message_id):
        await self.push(callback_query={
            "id": f"cb{self._alloc()}", "from": self.user(user_id), "chat_instance": "ci", "data": data,
            "message": {"message_id": message_id, "date": int(time.time()), "chat": {"id": chat_id, "type": "private"}, "text": "x"},
        })

    def _alloc(self):
        self._next_msg += 1
        return self._next_msg

    def _message(self, chat_id, text):
        mid = self._alloc()
        return {"message_id": mid, "date": int(time.time()), "chat": {"id": int(chat_id), "type": "private"},
                "from": BOT_USER, "text": text}

    async def handle(self, request):
        if request.match_info["token"] != self.token:
            return web.json_response({"ok": False, "error_code": 401, "description": "Unauthorized"}, status=401)
        method = request.match_info["method"]
        if request.content_type == "application/json":
            params = await request.json()
        else:
            params = {k: v for k, v in (await request.post()).items()}
        self.calls.append({"method": method, "params": params})
        result = True
        if method == "getMe":
            result = BOT_USER
        elif method == "getUpdates":
            result = await self._get_updates(params)
        elif method == "sendMessage":
            result = self._message(params["chat_id"], params.get("text", ""))
            self._store(result, params)
        elif method == "editMessageText":
            key = (int(params["chat_id"]), int(params["message_id"]))
            result = {**self.messages.get(key, self._message(params["chat_id"], "")), "text": params.get("text", "")}
            result["message_id"] = int(params["message_id"])
            self._store(result, params)
        return web.json_response({"ok": True, "result": result})

    def _store(self, msg, params):
        markup = json.loads(params["reply_markup"]) if params.get("reply_markup") else None
        self.messages[(msg["chat"]["id"], msg["message_id"])] = {**msg, "markup": markup, "params": params}

    async def _get_updates(self, params):
        offset = int(params.get("offset", 0) or 0)
        timeout = min(float(params.get("timeout", 0) or 0), 1.0)
        async with self._cond:
            self._updates = [u for u in self._updates if u["update_id"] >= offset]
            if not self._updates and timeout:
                try:
                    await asyncio.wait_for(self._cond.wait_for(lambda: bool(self._updates)), timeout)
                except asyncio.TimeoutError:
                    pass
            return list(self._updates)
