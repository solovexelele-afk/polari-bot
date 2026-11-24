"""Сайт POLARI: главная, магазин ролей и платформа «Проект в CS 2» (форум, магазин, новости, инфо, администрация).

Работает в том же aiohttp-сервере, что и health-check бота. Вход — через Discord OAuth2.
Покупка не списывает деньги на сайте: она создаёт заказ и приватный тикет в Discord,
где сотрудник согласует и подтверждает оплату (как в магазине бота).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urlencode, urlparse

import aiohttp
import jinja2
from aiohttp import web
from markupsafe import Markup, escape

from bot.data.blueprint import ROLES
from bot.data.content import CS2_ITEMS, CURRENCY, SHOP_BY_ID, SHOP_ITEMS
from bot.database.db import now_iso
from bot.utils.helpers import parse_color
from config import settings

if TYPE_CHECKING:
    from bot.core import PolariBot

log = logging.getLogger("polari.web")
WEB_DIR = Path(__file__).resolve().parent
BANNERS = settings.banner_path.parent
SESSION_COOKIE = "polari_session"
SESSION_TTL = 14 * 86400
POST_COOLDOWN = 20
DISCORD_API = "https://discord.com/api/v10"


def nl2br(text: str) -> Markup:
    return Markup("<br>".join(escape(line) for line in str(text).split("\n")))


class Site:
    def __init__(self, bot: "PolariBot"):
        self.bot = bot
        self.client_id = os.getenv("DISCORD_CLIENT_ID", "")
        self.client_secret = os.getenv("DISCORD_CLIENT_SECRET", "")
        self.site_url = (os.getenv("SITE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://localhost:8080").rstrip("/")
        secret = os.getenv("SESSION_SECRET")
        if not secret:
            log.warning("SESSION_SECRET не задан — входы на сайт будут сбрасываться при перезапуске.")
        self.secret = (secret or secrets.token_hex(32)).encode()
        self.invite = os.getenv("DISCORD_INVITE", "")
        self.cs2_ip = os.getenv("CS2_SERVER_IP", "")
        self._last_post: dict[int, float] = {}
        self.env = jinja2.Environment(loader=jinja2.FileSystemLoader(WEB_DIR / "templates"),
                                      autoescape=jinja2.select_autoescape(["html"]))
        self.env.filters["nl2br"] = nl2br
        self.env.filters["date"] = lambda iso: (iso or "")[:16].replace("T", " ")

    # ------------------------------------------------------------------ сессии (подписанная cookie)
    def _sign(self, payload: bytes) -> str:
        return hmac.new(self.secret, payload, hashlib.sha256).hexdigest()

    def read_session(self, request: web.Request) -> dict:
        raw = request.cookies.get(SESSION_COOKIE)
        if not raw or "." not in raw:
            return {}
        data, sig = raw.rsplit(".", 1)
        try:
            payload = base64.urlsafe_b64decode(data.encode())
        except ValueError:
            return {}
        if not hmac.compare_digest(self._sign(payload), sig):
            return {}
        session = json.loads(payload)
        return session if session.get("exp", 0) > time.time() else {}

    def write_session(self, response: web.StreamResponse, session: dict) -> None:
        session["exp"] = int(time.time()) + SESSION_TTL
        payload = json.dumps(session, separators=(",", ":")).encode()
        value = base64.urlsafe_b64encode(payload).decode() + "." + self._sign(payload)
        response.set_cookie(SESSION_COOKIE, value, max_age=SESSION_TTL, httponly=True, samesite="Lax",
                            secure=self.site_url.startswith("https"))

    # ------------------------------------------------------------------ данные Discord
    @property
    def guild(self):
        if settings.guild_id:
            return self.bot.get_guild(settings.guild_id)
        return self.bot.guilds[0] if self.bot.guilds else None

    async def viewer(self, request: web.Request) -> dict | None:
        session = self.read_session(request)
        user = session.get("user")
        if not user:
            return None
        guild = self.guild
        member = guild.get_member(int(user["id"])) if guild else None
        user["member"] = member is not None
        user["staff"] = bool(member) and await self.bot.registry.can(member, "admin")
        user["csrf"] = session.get("csrf", "")
        return user

    async def render(self, request: web.Request, template: str, status: int = 200, **ctx) -> web.Response:
        guild = self.guild
        ctx.setdefault("user", await self.viewer(request))
        ctx.update(guild_name=guild.name if guild else "POLARI", members=guild.member_count if guild else 0,
                   in_voice=sum(len(c.members) for c in guild.voice_channels) if guild else 0,
                   invite=self.invite, cs2_ip=self.cs2_ip, path=request.path, currency=CURRENCY,
                   bot_ready=self.bot.is_ready())
        html = self.env.get_template(template).render(**ctx)
        return web.Response(text=html, content_type="text/html", status=status)

    async def message(self, request: web.Request, title: str, text: str, *, platform: str = "polari",
                      status: int = 200, link: str | None = None, link_text: str = "Вернуться") -> web.Response:
        return await self.render(request, "message.html", status=status, title=title, text=text,
                                 platform=platform, link=link, link_text=link_text)

    async def require(self, request: web.Request, *, member: bool = True, staff: bool = False) -> dict:
        user = await self.viewer(request)
        if not user:
            back = request.path if request.method == "GET" else \
                (urlparse(request.headers.get("Referer", "")).path or "/")
            raise web.HTTPFound(f"/login?next={quote(back)}")
        if member and not user["member"]:
            raise web.HTTPForbidden(text="Сначала вступите на Discord-сервер POLARI.")
        if staff and not user["staff"]:
            raise web.HTTPForbidden(text="Доступно только администрации.")
        return user

    async def check_csrf(self, request: web.Request, user: dict) -> dict:
        form = await request.post()
        if not user.get("csrf") or not hmac.compare_digest(str(form.get("csrf", "")), user["csrf"]):
            raise web.HTTPForbidden(text="Сессия устарела, обновите страницу.")
        return dict(form)

    def cooldown(self, user_id: int) -> int:
        left = POST_COOLDOWN - (time.monotonic() - self._last_post.get(user_id, 0))
        return int(left) if left > 0 else 0

    # ------------------------------------------------------------------ авторизация
    async def login(self, request: web.Request) -> web.Response:
        if not self.client_id and self.bot.user:
            self.client_id = str(self.bot.user.id)  # у бот-приложения ID клиента совпадает с ID бота
        if not (self.client_id and self.client_secret):
            return await self.message(request, "Вход пока недоступен",
                                      "Администратор ещё не настроил вход через Discord на сайте.")
        state = secrets.token_urlsafe(16)
        nxt = request.query.get("next", "/")
        params = {"client_id": self.client_id, "response_type": "code", "scope": "identify", "prompt": "none",
                  "redirect_uri": f"{self.site_url}/auth/callback", "state": state}
        response = web.HTTPFound("https://discord.com/oauth2/authorize?" + urlencode(params))
        session = self.read_session(request)
        session.update(state=state, next=nxt if nxt.startswith("/") else "/")
        self.write_session(response, session)
        return response

    async def callback(self, request: web.Request) -> web.Response:
        session = self.read_session(request)
        if not request.query.get("code") or request.query.get("state") != session.get("state"):
            return await self.message(request, "Вход не выполнен", "Ссылка входа устарела. Попробуйте ещё раз.",
                                      link="/login", link_text="Войти снова")
        data = {"client_id": self.client_id, "client_secret": self.client_secret, "grant_type": "authorization_code",
                "code": request.query["code"], "redirect_uri": f"{self.site_url}/auth/callback"}
        async with aiohttp.ClientSession() as http:
            async with http.post(f"{DISCORD_API}/oauth2/token", data=data) as resp:
                token = await resp.json()
            if "access_token" not in token:
                log.warning("OAuth: обмен кода не удался: %s", token.get("error"))
                return await self.message(request, "Вход не выполнен", "Discord не подтвердил вход. Попробуйте ещё раз.",
                                          link="/login", link_text="Войти снова")
            async with http.get(f"{DISCORD_API}/users/@me",
                                headers={"Authorization": f"Bearer {token['access_token']}"}) as resp:
                me = await resp.json()
        avatar = (f"https://cdn.discordapp.com/avatars/{me['id']}/{me['avatar']}.png?size=128" if me.get("avatar")
                  else "https://cdn.discordapp.com/embed/avatars/0.png")
        response = web.HTTPFound(session.get("next", "/"))
        self.write_session(response, {"user": {"id": me["id"], "name": me.get("global_name") or me["username"],
                                               "avatar": avatar}, "csrf": secrets.token_urlsafe(24)})
        return response

    async def logout(self, request: web.Request) -> web.Response:
        response = web.HTTPFound("/")
        response.del_cookie(SESSION_COOKIE)
        return response

    # ------------------------------------------------------------------ POLARI
    async def index(self, request: web.Request) -> web.Response:
        return await self.render(request, "polari/index.html", items=SHOP_ITEMS[:3])

    async def shop(self, request: web.Request) -> web.Response:
        return await self.render(request, "polari/shop.html", items=SHOP_ITEMS)

    async def buy(self, request: web.Request) -> web.Response:
        platform = "cs2" if request.path.startswith("/cs2") else "polari"
        user = await self.require(request)
        form = await self.check_csrf(request, user)
        item = SHOP_BY_ID.get(str(form.get("item_id", "")))
        if not item or (platform == "cs2") != (item.kind == "cs2"):
            return await self.message(request, "Товар не найден", "Обновите страницу магазина.", platform=platform,
                                      status=404)
        custom_name = custom_color = extra = None
        if item.custom:
            custom_name = str(form.get("custom_name", "")).strip()[:32]
            color = parse_color(str(form.get("custom_color", "")))
            if not custom_name or "@" in custom_name or color is None:
                return await self.message(request, "Проверьте данные", "Укажите название роли и цвет в формате #RRGGBB.",
                                          platform=platform, link=request.headers.get("Referer"))
            custom_color = f"{color:06X}"
        if item.kind == "cs2":
            extra = str(form.get("steam", "")).strip()[:120]
            if len(extra) < 3:
                return await self.message(request, "Укажите аккаунт", "Впишите SteamID или ссылку на профиль Steam.",
                                          platform=platform, link=request.headers.get("Referer"))
        guild = self.guild
        member = guild.get_member(int(user["id"])) if guild else None
        shop = self.bot.get_cog("ShopCog")
        if not member or not shop:
            return await self.message(request, "Магазин недоступен", "Бот сейчас перезапускается, попробуйте позже.",
                                      platform=platform, status=503)
        from bot.cogs.shop import OrderError
        try:
            order_id, channel = await shop.place_order(member, item, custom_name, custom_color, extra, source="site")
        except OrderError as exc:
            return await self.message(request, "Заказ не создан", str(exc), platform=platform)
        return await self.render(request, "order.html", platform=platform, order_id=order_id, item=item,
                                 ticket_url=channel.jump_url)

    # ------------------------------------------------------------------ CS 2
    async def news_list(self, limit: int = 20):
        return await self.bot.db.fetchall("SELECT * FROM site_news WHERE section='cs2' ORDER BY id DESC LIMIT ?", (limit,))

    async def cs2_home(self, request: web.Request) -> web.Response:
        threads = await self.bot.db.fetchall(
            "SELECT t.*, c.title AS cat_title, c.icon FROM forum_threads t JOIN forum_categories c ON c.id=t.category_id "
            "ORDER BY t.updated_at DESC LIMIT 5")
        return await self.render(request, "cs2/home.html", news=await self.news_list(3), threads=threads,
                                 items=CS2_ITEMS[:3])

    async def cs2_forum(self, request: web.Request) -> web.Response:
        cats = await self.bot.db.fetchall(
            "SELECT c.*, (SELECT COUNT(*) FROM forum_threads t WHERE t.category_id=c.id) AS threads, "
            "(SELECT COALESCE(SUM(replies)+COUNT(*),0) FROM forum_threads t WHERE t.category_id=c.id) AS posts, "
            "(SELECT title FROM forum_threads t WHERE t.category_id=c.id ORDER BY updated_at DESC LIMIT 1) AS last_title, "
            "(SELECT id FROM forum_threads t WHERE t.category_id=c.id ORDER BY updated_at DESC LIMIT 1) AS last_id "
            "FROM forum_categories c ORDER BY position")
        return await self.render(request, "cs2/forum.html", cats=cats)

    async def _category(self, slug: str):
        cat = await self.bot.db.fetchone("SELECT * FROM forum_categories WHERE slug=?", (slug,))
        if not cat:
            raise web.HTTPNotFound(text="Раздел не найден")
        return cat

    async def cs2_category(self, request: web.Request) -> web.Response:
        cat = await self._category(request.match_info["slug"])
        threads = await self.bot.db.fetchall(
            "SELECT * FROM forum_threads WHERE category_id=? ORDER BY pinned DESC, updated_at DESC LIMIT 100", (cat["id"],))
        return await self.render(request, "cs2/category.html", cat=cat, threads=threads)

    async def cs2_new_thread(self, request: web.Request) -> web.Response:
        cat = await self._category(request.match_info["slug"])
        user = await self.require(request)
        form = await self.check_csrf(request, user)
        if cat["staff_only"] and not user["staff"]:
            raise web.HTTPForbidden(text="В этом разделе пишет только администрация.")
        title = str(form.get("title", "")).strip()[:120]
        content = str(form.get("content", "")).strip()[:8000]
        if len(title) < 3 or len(content) < 3:
            return await self.message(request, "Заполните тему", "Заголовок и текст — минимум 3 символа.",
                                      platform="cs2", link=f"/cs2/forum/{cat['slug']}")
        if wait := self.cooldown(int(user["id"])):
            return await self.message(request, "Не так быстро", f"Новую запись можно отправить через {wait} сек.",
                                      platform="cs2", link=f"/cs2/forum/{cat['slug']}")
        now = now_iso()
        tid = await self.bot.db.execute(
            "INSERT INTO forum_threads (category_id,title,author_id,author_name,author_avatar,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)", (cat["id"], title, int(user["id"]), user["name"], user["avatar"], now, now))
        await self.bot.db.execute(
            "INSERT INTO forum_posts (thread_id,author_id,author_name,author_avatar,content,created_at) VALUES (?,?,?,?,?,?)",
            (tid, int(user["id"]), user["name"], user["avatar"], content, now))
        self._last_post[int(user["id"])] = time.monotonic()
        raise web.HTTPFound(f"/cs2/forum/t/{tid}")

    async def _thread(self, tid: int):
        thread = await self.bot.db.fetchone(
            "SELECT t.*, c.slug, c.title AS cat_title FROM forum_threads t JOIN forum_categories c ON c.id=t.category_id "
            "WHERE t.id=?", (tid,))
        if not thread:
            raise web.HTTPNotFound(text="Тема не найдена")
        return thread

    async def cs2_thread(self, request: web.Request) -> web.Response:
        thread = await self._thread(int(request.match_info["tid"]))
        posts = await self.bot.db.fetchall("SELECT * FROM forum_posts WHERE thread_id=? ORDER BY id", (thread["id"],))
        return await self.render(request, "cs2/thread.html", thread=thread, posts=posts)

    async def cs2_reply(self, request: web.Request) -> web.Response:
        thread = await self._thread(int(request.match_info["tid"]))
        user = await self.require(request)
        form = await self.check_csrf(request, user)
        if thread["locked"] and not user["staff"]:
            raise web.HTTPForbidden(text="Тема закрыта.")
        content = str(form.get("content", "")).strip()[:8000]
        if len(content) < 2:
            raise web.HTTPFound(f"/cs2/forum/t/{thread['id']}")
        if wait := self.cooldown(int(user["id"])):
            return await self.message(request, "Не так быстро", f"Ответ можно отправить через {wait} сек.",
                                      platform="cs2", link=f"/cs2/forum/t/{thread['id']}")
        now = now_iso()
        await self.bot.db.execute(
            "INSERT INTO forum_posts (thread_id,author_id,author_name,author_avatar,content,created_at) VALUES (?,?,?,?,?,?)",
            (thread["id"], int(user["id"]), user["name"], user["avatar"], content, now))
        await self.bot.db.execute("UPDATE forum_threads SET replies=replies+1, updated_at=? WHERE id=?", (now, thread["id"]))
        self._last_post[int(user["id"])] = time.monotonic()
        raise web.HTTPFound(f"/cs2/forum/t/{thread['id']}#last")

    async def cs2_moderate(self, request: web.Request) -> web.Response:
        thread = await self._thread(int(request.match_info["tid"]))
        user = await self.require(request, staff=True)
        form = await self.check_csrf(request, user)
        action = form.get("action")
        db = self.bot.db
        if action == "pin":
            await db.execute("UPDATE forum_threads SET pinned=1-pinned WHERE id=?", (thread["id"],))
        elif action == "lock":
            await db.execute("UPDATE forum_threads SET locked=1-locked WHERE id=?", (thread["id"],))
        elif action == "delete":
            await db.execute("DELETE FROM forum_posts WHERE thread_id=?", (thread["id"],))
            await db.execute("DELETE FROM forum_threads WHERE id=?", (thread["id"],))
            raise web.HTTPFound(f"/cs2/forum/{thread['slug']}")
        elif action == "delete_post" and str(form.get("post_id", "")).isdigit():
            await db.execute("DELETE FROM forum_posts WHERE id=? AND thread_id=?", (int(form["post_id"]), thread["id"]))
            await db.execute("UPDATE forum_threads SET replies=MAX(replies-1,0) WHERE id=?", (thread["id"],))
        raise web.HTTPFound(f"/cs2/forum/t/{thread['id']}")

    async def cs2_shop(self, request: web.Request) -> web.Response:
        return await self.render(request, "cs2/shop.html", items=CS2_ITEMS)

    async def cs2_news(self, request: web.Request) -> web.Response:
        return await self.render(request, "cs2/news.html", news=await self.news_list(50))

    async def cs2_news_post(self, request: web.Request) -> web.Response:
        user = await self.require(request, staff=True)
        form = await self.check_csrf(request, user)
        if form.get("delete_id", "").isdigit():
            await self.bot.db.execute("DELETE FROM site_news WHERE id=?", (int(form["delete_id"]),))
            raise web.HTTPFound("/cs2/news")
        title = str(form.get("title", "")).strip()[:150]
        content = str(form.get("content", "")).strip()[:10000]
        if title and content:
            await self.bot.db.execute(
                "INSERT INTO site_news (section,title,content,author_id,author_name,created_at) VALUES ('cs2',?,?,?,?,?)",
                (title, content, int(user["id"]), user["name"], now_iso()))
        raise web.HTTPFound("/cs2/news")

    async def cs2_info(self, request: web.Request) -> web.Response:
        return await self.render(request, "cs2/info.html")

    async def cs2_admins(self, request: web.Request) -> web.Response:
        guild = self.guild
        groups = []
        if guild:
            seen: set[int] = set()
            for spec in ROLES:
                if not spec.staff or spec.key == "staff":
                    continue
                role = await self.bot.registry.role(guild, spec.key)
                if not role:
                    continue
                people = [m for m in role.members if not m.bot and m.id not in seen]
                seen.update(m.id for m in people)
                if people:
                    groups.append({"name": role.name, "color": f"#{role.colour.value:06x}" if role.colour.value else "#9fb3d1",
                                   "members": [{"name": m.display_name, "avatar": m.display_avatar.url}
                                               for m in people]})
        return await self.render(request, "cs2/admins.html", groups=groups)

    # ------------------------------------------------------------------ ошибки
    @web.middleware
    async def errors(self, request: web.Request, handler):
        try:
            return await handler(request)
        except web.HTTPException as exc:
            if exc.status not in (403, 404):
                raise  # редиректы и прочее — как есть
            title = "Доступ запрещён" if exc.status == 403 else "Страница не найдена"
            text = exc.text if exc.text and not exc.text[:3].isdigit() else \
                "Такой страницы нет — возможно, она была удалена."
            platform = "cs2" if request.path.startswith("/cs2") else "polari"
            return await self.message(request, title, text, platform=platform, status=exc.status,
                                      link="/cs2" if platform == "cs2" else "/")
        except Exception:
            log.exception("Ошибка страницы %s", request.path)
            return await self.message(request, "Что-то пошло не так", "Ошибка записана в журнал, мы разберёмся.",
                                      status=500)

    def routes(self, app: web.Application) -> None:
        app.middlewares.append(self.errors)
        r = app.router
        r.add_get("/", self.index)
        r.add_get("/shop", self.shop)
        r.add_post("/shop/buy", self.buy)
        r.add_get("/login", self.login)
        r.add_get("/auth/callback", self.callback)
        r.add_get("/logout", self.logout)
        r.add_get("/cs2", self.cs2_home)
        r.add_get("/cs2/forum", self.cs2_forum)
        r.add_get("/cs2/forum/t/{tid:\\d+}", self.cs2_thread)
        r.add_post("/cs2/forum/t/{tid:\\d+}/reply", self.cs2_reply)
        r.add_post("/cs2/forum/t/{tid:\\d+}/mod", self.cs2_moderate)
        r.add_get("/cs2/forum/{slug}", self.cs2_category)
        r.add_post("/cs2/forum/{slug}/new", self.cs2_new_thread)
        r.add_get("/cs2/shop", self.cs2_shop)
        r.add_post("/cs2/shop/buy", self.buy)
        r.add_get("/cs2/news", self.cs2_news)
        r.add_post("/cs2/news", self.cs2_news_post)
        r.add_get("/cs2/info", self.cs2_info)
        r.add_get("/cs2/admins", self.cs2_admins)
        r.add_static("/static", WEB_DIR / "static")
        r.add_static("/banners", BANNERS)
