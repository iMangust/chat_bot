"""Функциональный тест REST API панели: все ручки с токеном и без."""
import asyncio, os, sys, tempfile

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["REDIS_URL"] = ""  # FSM in-memory не нужен для API
os.environ["BOT_TOKEN"] = "123456:TESTTESTTEST"
os.environ["WEBHOOK_SECRET_TOKEN"] = "SecRetTokEn123"
sys.path.insert(0, ".")

# Сброс lru-кеша настроек (тесты идут в одном pytest-процессе)
from app.config import get_settings as _gs  # noqa: E402
_gs.cache_clear()

from app.db.session import engine, session_factory  # noqa: E402
from app.db.models import Base  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # наполним данные
    from app.db.repositories import MerchRepository, EventRepository, UserRepository
    async with session_factory() as s:
        u = await UserRepository(s).get_or_create(777, "Тест", "testuser")
        m = MerchRepository(s)
        c = await m.add_category("clothes", "Одежда", "🧢", 0)
        p = await m.add_product(c.id, "Кепка", "тест", None)
        v, created = await m.add_variant(p.id, "M", "чёрный", 990, 5)
        assert created and v.id is not None
        e = await EventRepository(s).create(title="Встреча", date="2026-10-05", time="18:00")
        await s.commit()
        uid, pid, vid, eid = u.tg_id, p.id, v.id, e.id

    from app.web.admin_api import router
    app = FastAPI()
    app.include_router(router)
    tr = ASGITransport(app=app)

    H = {"X-Dashboard-Token": "SecRetTokEn123"}
    async with AsyncClient(transport=tr, base_url="http://t") as cl:
        # 1) без токена — 401 на всех ручках
        r = await cl.get("/api/stats/overview")
        assert r.status_code == 401, ("no-token stats", r.status_code, r.text)
        r = await cl.post("/api/events", json={"title": "x"})
        assert r.status_code == 401, ("no-token write", r.status_code)

        # 2) токен (не-localhost peer у ASGITransport = testserver; проверка local or allowed)
        r = await cl.get("/api/token")
        assert r.status_code == 200, ("token via panel", r.status_code)
        H = {"X-Dashboard-Token": r.json()["token"]}
        assert H["X-Dashboard-Token"], "empty token"

        # 3) со валидным токеном — читаем всё
        for path in ["/api/stats/overview", "/api/users?q=test", f"/api/users/{uid}",
                     "/api/pets", "/api/achievements?with_holders=true", "/api/merch",
                     "/api/merch/reservations", "/api/events", "/api/subscribers",
                     "/api/notifications"]:
            r = await cl.get(path, headers=H)
            assert r.status_code == 200, (path, r.status_code, r.text[:200])

        d = (await cl.get("/api/stats/overview", headers=H)).json()
        assert d["usersTotal"] >= 1 and d["events"] >= 1 and d["topUsers"], d

        # 4) CRUD мерча через API
        r = await cl.post("/api/merch/categories", headers=H,
                          json={"code": "acc", "title": "Аксессуары", "icon": "🎒", "position": 1})
        assert r.status_code == 200, r.text
        r = await cl.post("/api/merch/products", headers=H,
                          json={"category_id": r.json()["id"], "name": "Носки", "description": "", "image_url": None})
        assert r.status_code == 200, r.text
        prod_id = r.json()["id"]
        r = await cl.post("/api/merch/variants", headers=H,
                          json={"product_id": prod_id, "size": "42", "color": "белый", "price_rub": 300, "stock": 3})
        assert r.status_code == 200 and r.json()["id"], r.text
        new_vid = r.json()["id"]
        r = await cl.post(f"/api/merch/variants/{new_vid}", headers=H,
                          json={"price_rub": 350, "stock": 10})
        assert r.status_code == 200, r.text
        merch = (await cl.get("/api/merch", headers=H)).json()
        flat = [v for c in merch["categories"] for p in c["products"] for v in p["variants"]]
        assert any(v["id"] == new_vid and v["priceRub"] == 350 for v in flat), merch
        r = await cl.delete(f"/api/merch/variants/{new_vid}", headers=H)
        assert r.status_code == 200, r.text

        # 5) CRUD мероприятий
        r = await cl.post("/api/events", headers=H, json={"title": "Пикник", "date": "2026-11-01"})
        assert r.status_code == 200, r.text
        nid = r.json()["id"]
        r = await cl.post(f"/api/events/{nid}", headers=H, json={"title": "Пикник-2", "place": "Парк"})
        assert r.status_code == 200, r.text
        evs = (await cl.get("/api/events", headers=H)).json()["items"]
        assert any(e["id"] == nid and e["title"] == "Пикник-2" and e["place"] == "Парк" for e in evs), evs
        r = await cl.delete(f"/api/events/{nid}", headers=H)
        assert r.status_code == 200, r.text

        # 6) пользователи: бан/редактирование
        r = await cl.post(f"/api/users/{uid}/ban", headers=H, json={"banned": True})
        assert r.status_code == 200, r.text
        det = (await cl.get(f"/api/users/{uid}", headers=H)).json()
        assert det["user"]["banned"] is True
        r = await cl.post(f"/api/users/{uid}/edit", headers=H, json={"xp": 500, "coins": 50, "level": 7})
        assert r.status_code == 200, r.text
        det = (await cl.get(f"/api/users/{uid}", headers=H)).json()
        assert det["user"]["xp"] == 500 and det["user"]["level"] == 7, det["user"]
        # 404
        r = await cl.get("/api/users/999999999", headers=H)
        assert r.status_code == 404

        # 7) очистка
        r = await cl.delete("/api/merch/categories/acc", headers=H)
        assert r.status_code == 200, r.text
        r = await cl.delete(f"/api/merch/products/{prod_id}", headers=H)
        assert r.status_code in (200, 404)  # категория уже удалена каскадом — допустимо

    print("ADMIN_API_ALL_OK")


asyncio.run(main())
