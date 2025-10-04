import asyncio
import platform
import subprocess
from datetime import datetime, time
from pathlib import Path
import json
import os
import html  # ⬅️ ganti helper escape

import yaml
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.request import HTTPXRequest

# For daily heartbeat timezone (Asia/Jakarta)
try:
    from zoneinfo import ZoneInfo
    TZ_JAKARTA = ZoneInfo("Asia/Jakarta")
except Exception:
    TZ_JAKARTA = None  # fallback to server local time if zoneinfo unavailable

# -------- Util --------
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def is_windows():
    return "windows" in platform.system().lower()

async def ping_host(host: str, timeout_sec: float = 1.2) -> bool:
    """Ping 1x dengan timeout singkat agar loop cepat selesai."""
    try:
        if is_windows():
            # -n 1 sekali, -w timeout ms
            cmd = ["ping", "-n", "1", "-w", str(int(timeout_sec * 1000)), host]
        else:
            cmd = ["ping", "-c", "1", "-W", str(int(timeout_sec)), host]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        rc = await proc.wait()
        return rc == 0
    except Exception:
        return False

# -------- State --------
STATE_FILE = Path("state.json")
SUB_FILE  = Path("subscribers.json")

def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default

def save_json(path: Path, obj):
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")

# -------- Bot --------
class BotApp:
    def __init__(self):
        load_dotenv()
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.sender = os.getenv("SENDER_NAME", "CCTV Ping Monitor")
        if not token:
            raise RuntimeError("Token bot tidak ada di .env")

        # --- Admins ---
        admins = os.getenv("ADMIN_CHAT_IDS", "")
        self.admin_ids = {int(x) for x in admins.split(",") if x.strip().isdigit()}
        if not self.admin_ids:
            raise RuntimeError("Set dulu ADMIN_CHAT_IDS di .env")

        # --- muat config pertama kali ---
        self._load_config()

        # --- State & subscribers/pending ---
        self.state = load_json(STATE_FILE, {})
        subs_doc = load_json(SUB_FILE, {})
        self.subscribers = set(subs_doc.get("subs", []))
        self.pending = set(subs_doc.get("pending", []))

        # pastikan semua kamera punya entry state
        for cam in self.cameras:
            nm = cam["name"]
            if nm not in self.state:
                self.state[nm] = {
                    "status": "UNKNOWN", "fails": 0, "succ": 0,
                    "last_alert_ts": 0, "last_up_ts": 0, "last_down_ts": 0
                }

        # HTTPXRequest
        request = HTTPXRequest(
            connect_timeout=20.0,
            read_timeout=60.0,
            write_timeout=60.0,
            pool_timeout=10.0,
        )
        self.app = Application.builder().token(token).request(request).build()

        # Commands
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("stop",  self.cmd_stop))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("testalert", self.cmd_testalert))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("pending", self.cmd_pending))
        self.app.add_handler(CommandHandler("reload", self.cmd_reload))      # HOT RELOAD (admin)
        self.app.add_handler(CommandHandler("ping", self.cmd_ping))          # PILIH KAMERA / ARGUMEN + ANIMASI
        self.app.add_handler(CallbackQueryHandler(self.cb_approval, pattern="^(approve|deny):"))
        self.app.add_handler(CallbackQueryHandler(self.cb_ping, pattern="^ping:"))

        # Background monitoring job (disimpan handlernya utk bisa di-reschedule saat reload)
        self.monitor_job = self.app.job_queue.run_repeating(
            self.monitor_tick,
            interval=self.poll,
            first=3,
            job_kwargs={
                "max_instances": 1,
                "coalesce": True,
                "misfire_grace_time": 5,
            },
        )

        # Daily heartbeat (08:00 WIB)
        send_time = time(hour=8, minute=0, tzinfo=TZ_JAKARTA) if TZ_JAKARTA else time(hour=8, minute=0)
        self.heartbeat_job = self.app.job_queue.run_daily(self.daily_heartbeat, send_time)

        # Error handler
        async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
            print(f"[{now_str()}] ERROR: {context.error}")
        self.app.add_error_handler(on_error)

    # ===== Config loader & helpers =====
    def _load_config(self):
        """Load cameras.yaml & apply defaults."""
        cfg = yaml.safe_load(Path("cameras.yaml").read_text(encoding="utf-8"))
        self.poll    = int(cfg.get("poll_interval_seconds", 10))
        self.fail_th = int(cfg.get("fail_threshold", 3))
        self.rec_th  = int(cfg.get("recover_threshold", 2))
        self.cooldown_default = int(cfg.get("cooldown_seconds", 600))  # NEW
        cams = cfg.get("cameras", [])
        # normalisasi per camera
        self.cameras = []
        for c in cams:
            self.cameras.append({
                "name": c["name"],
                "host": c["host"],
                "cooldown_seconds": int(c.get("cooldown_seconds", self.cooldown_default))
            })

    def _save_subs(self):
        save_json(SUB_FILE, {"subs": list(self.subscribers), "pending": list(self.pending)})

    def is_authorized(self, chat_id: int) -> bool:
        return (chat_id in self.subscribers) or (chat_id in self.admin_ids)

    # ===== Animasi util =====
    async def _safe_edit(self, chat_id: int, message_id: int, html_text: str):
        try:
            await self.app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=html_text,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            # abaikan error ringan (MessageNotModified / race edit)
            pass

    async def _animate_and_ping(self, chat_id: int, message_id: int, cam_name: str, host: str):
        base = f"🔍 Pinging <b>{html.escape(cam_name)}</b>"
        for dots in [".", "..", "..."]:
            await asyncio.sleep(0.6)
            await self._safe_edit(chat_id, message_id, base + dots)

        ok = await ping_host(host)
        emoji = "✅" if ok else "❌"
        await self._safe_edit(
            chat_id, message_id,
            f"{emoji} <b>{html.escape(cam_name)}</b> — {'UP' if ok else 'DOWN'}"
        )

    # ===== Commands =====
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if chat_id in self.admin_ids:
            return await update.message.reply_html("👑 Kamu admin. Akses penuh diberikan. Ketik /help.")
        if chat_id in self.subscribers:
            return await update.message.reply_html("✅ Kamu sudah disetujui. Ketik /help.")
        if chat_id in self.pending:
            return await update.message.reply_html("⏳ Permintaanmu masih menunggu persetujuan admin.")

        # ajukan permintaan baru
        self.pending.add(chat_id)
        self._save_subs()
        await update.message.reply_html("📨 Permintaan akses dikirim ke admin. Tunggu disetujui, ya.")

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{chat_id}"),
            InlineKeyboardButton("❌ Deny",    callback_data=f"deny:{chat_id}")
        ]])
        for adm in self.admin_ids:
            try:
                await self.app.bot.send_message(
                    adm,
                    f"🆕 Permintaan akses dari <code>{chat_id}</code> ({update.effective_user.full_name}).",
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb
                )
            except Exception as e:
                print(f"[{now_str()}] Gagal kirim ke admin {adm}: {e}")

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if chat_id in self.subscribers:
            self.subscribers.discard(chat_id)
            self._save_subs()
            return await update.message.reply_html("⏹️ Berhenti terima notifikasi.")
        if chat_id in self.pending:
            self.pending.discard(chat_id)
            self._save_subs()
            return await update.message.reply_html("🗑️ Permintaanmu dibatalkan.")
        await update.message.reply_html("ℹ️ Kamu belum terdaftar.")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_authorized(update.effective_chat.id):
            return await update.message.reply_html("🚫 Akses ditolak. Ketik /start untuk ajukan izin.")
        lines = []
        for cam in self.cameras:
            st = self.state.get(cam["name"], {}).get("status", "UNKNOWN")
            emoji = "✅" if st == "UP" else ("❌" if st == "DOWN" else "❔")
            lines.append(f"{emoji} <b>{html.escape(cam['name'])}</b> — {st}")
        await update.message.reply_html("\n".join(lines) if lines else "Belum ada kamera terdaftar.")

    async def cmd_testalert(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id not in self.admin_ids:
            return await update.message.reply_html("🚫 Khusus admin.")
        await self.broadcast("🔔 Test alert dari bot – jalur Telegram OK.")

    async def cmd_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id not in self.admin_ids:
            return await update.message.reply_html("🚫 Khusus admin.")
        if not self.pending:
            return await update.message.reply_html("✅ Tidak ada permintaan pending.")
        await update.message.reply_html(
            "⏳ Pending:\n" + "\n".join([f"- <code>{i}</code>" for i in sorted(self.pending)])
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if chat_id in self.admin_ids:
            msg = (
                "👑 <b>Daftar Command (Admin)</b>\n\n"
                "/start - Aktifkan bot (admin langsung approved)\n"
                "/status - Lihat status semua kamera\n"
                "/ping [nama] - Ping satu kamera; tanpa argumen akan muncul pilihan (dengan animasi)\n"
                "/testalert - Kirim pesan uji coba ke semua subscriber\n"
                "/pending - Lihat daftar user yang masih pending\n"
                "/reload - Baca ulang cameras.yaml & reschedule monitor\n"
                "/stop - Berhenti menerima notifikasi\n"
                "/help - Menampilkan bantuan ini\n"
            )
        elif chat_id in self.subscribers:
            msg = (
                "✅ <b>Daftar Command (User Approved)</b>\n\n"
                "/status - Lihat status semua kamera\n"
                "/ping [nama] - Ping satu kamera; tanpa argumen akan muncul pilihan (dengan animasi)\n"
                "/stop - Berhenti menerima notifikasi\n"
                "/help - Menampilkan bantuan ini\n"
            )
        else:
            msg = (
                "ℹ️ <b>Daftar Command (User Belum Approved)</b>\n\n"
                "/start - Ajukan permintaan akses ke admin\n"
                "/help - Menampilkan bantuan ini\n\n"
                "⚠️ Kamu perlu disetujui admin dulu sebelum bisa pakai command lain."
            )
        await update.message.reply_html(msg)

    async def cmd_reload(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Hot reload config: admin only. Re-load cameras.yaml, reschedule monitor job jika interval berubah,
           dan inisialisasi state kamera baru."""
        if update.effective_chat.id not in self.admin_ids:
            return await update.message.reply_html("🚫 Khusus admin.")
        old_interval = self.poll
        try:
            self._load_config()
            # Tambah state untuk kamera baru
            for cam in self.cameras:
                nm = cam["name"]
                if nm not in self.state:
                    self.state[nm] = {
                        "status": "UNKNOWN", "fails": 0, "succ": 0,
                        "last_alert_ts": 0, "last_up_ts": 0, "last_down_ts": 0
                    }
            save_json(STATE_FILE, self.state)

            # Reschedule monitor jika interval berubah
            if self.monitor_job:
                self.monitor_job.schedule_removal()
            self.monitor_job = self.app.job_queue.run_repeating(
                self.monitor_tick,
                interval=self.poll,
                first=3,
                job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 5},
            )
            await update.message.reply_html(
                f"♻️ Reload sukses.\n"
                f"- Interval: {old_interval}s → {self.poll}s\n"
                f"- Kamera terdaftar: {len(self.cameras)}"
            )
        except Exception as e:
            await update.message.reply_html(f"❌ Reload gagal: <code>{e}</code>")

    async def cmd_ping(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/ping [nama] — jika tanpa argumen, tampilkan pilihan kamera (inline keyboard) dan gunakan animasi pada hasil."""
        if not self.is_authorized(update.effective_chat.id):
            return await update.message.reply_html("🚫 Akses ditolak.")
        args = context.args
        # Jika ada argumen → cari exact match by name, lalu ANIMASI
        if args:
            name = " ".join(args).strip()
            cam = next((c for c in self.cameras if c["name"].lower() == name.lower()), None)
            if not cam:
                return await update.message.reply_html("❌ Kamera tidak ditemukan. Ketik /ping tanpa argumen untuk memilih.")
            # kirim pesan awal lalu animasi + hasil
            msg = await update.message.reply_html(f"🔍 Pinging <b>{html.escape(cam['name'])}</b>")
            await self._animate_and_ping(update.effective_chat.id, msg.message_id, cam["name"], cam["host"])
            return
        # Tanpa argumen → tampilkan pilihan kamera (dibagi per baris 2 tombol)
        buttons = []
        row = []
        for i, cam in enumerate(self.cameras, start=1):
            row.append(InlineKeyboardButton(cam["name"], callback_data=f"ping:{cam['name']}"))
            if len(row) == 2:
                buttons.append(row); row = []
        if row:
            buttons.append(row)
        kb = InlineKeyboardMarkup(buttons) if buttons else None
        await update.message.reply_html("Pilih kamera untuk di-ping:", reply_markup=kb)

    # -------- Callback tombol admin approve/deny --------
    async def cb_approval(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        if q.from_user.id not in self.admin_ids:
            return await q.edit_message_text("🚫 Hanya admin yang boleh melakukan ini.")
        try:
            action, chat_id_str = q.data.split(":", 1)
            target = int(chat_id_str)
        except Exception:
            return await q.edit_message_text("Format data tidak valid.")

        if action == "approve":
            if target in self.pending:
                self.pending.discard(target)
                self.subscribers.add(target)
                self._save_subs()
                await q.edit_message_text(f"✅ Disetujui: {target}")
                try:
                    await self.app.bot.send_message(
                        target,
                        "✅ Akses disetujui. Sekarang kamu bisa pakai /status, /ping dan menerima alarm."
                    )
                except Exception as e:
                    print(f"[{now_str()}] Notif ke {target} gagal: {e}")
            else:
                await q.edit_message_text("ℹ️ Tidak ada permintaan yang cocok.")
        elif action == "deny":
            if target in self.pending:
                self.pending.discard(target)
                self._save_subs()
                await q.edit_message_text(f"❌ Ditolak: {target}")
                try:
                    await self.app.bot.send_message(target, "❌ Maaf, permintaan aksesmu ditolak admin.")
                except Exception:
                    pass
            else:
                await q.edit_message_text("ℹ️ Tidak ada permintaan yang cocok.")

    # -------- Callback tombol ping (pilih kamera) --------
    async def cb_ping(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        if not self.is_authorized(q.from_user.id):
            return await q.edit_message_text("🚫 Akses ditolak.")
        try:
            _, cam_name = q.data.split(":", 1)
        except Exception:
            return await q.edit_message_text("Format data tidak valid.")
        cam = next((c for c in self.cameras if c["name"] == cam_name), None)
        if not cam:
            return await q.edit_message_text("❌ Kamera tidak ditemukan (konfigurasi mungkin berubah).")

        chat_id = q.message.chat.id
        message_id = q.message.message_id

        # Ubah pesan tombol menjadi teks ping + animasi + hasil di pesan yang sama
        await self._safe_edit(chat_id, message_id, f"🔍 Pinging <b>{html.escape(cam['name'])}</b>")
        await self._animate_and_ping(chat_id, message_id, cam["name"], cam["host"])

    # -------- Monitoring --------
    async def monitor_tick(self, context: ContextTypes.DEFAULT_TYPE):
        """Ping semua kamera secara paralel, proses hasil, dan kirim notifikasi pada transisi
           dengan menerapkan cooldown per kamera."""
        if not self.cameras:
            return

        sem = asyncio.Semaphore(50)

        async def ping_with_sem(host: str):
            async with sem:
                return await ping_host(host, timeout_sec=1.2)

        tasks = [ping_with_sem(cam["host"]) for cam in self.cameras]
        results = await asyncio.gather(*tasks, return_exceptions=False)

        changed = False
        now_ts = int(datetime.now().timestamp())

        for cam, ok in zip(self.cameras, results):
            name, host = cam["name"], cam["host"]
            cooldown = int(cam.get("cooldown_seconds", self.cooldown_default))
            entry = self.state.setdefault(name, {
                "status": "UNKNOWN", "fails": 0, "succ": 0,
                "last_alert_ts": 0, "last_up_ts": 0, "last_down_ts": 0
            })
            prev = entry["status"]

            if ok:
                entry["succ"] += 1
                entry["fails"] = 0
                if prev in ("DOWN", "UNKNOWN") and entry["succ"] >= self.rec_th:
                    entry["status"] = "UP"
                    entry["last_up_ts"] = now_ts
                    changed = True
                    # Cooldown check
                    if now_ts - entry.get("last_alert_ts", 0) >= cooldown:
                        await self.broadcast(
                            f"📷 <b>{html.escape(name)}</b> kembali UP ✅\nHost: <code>{html.escape(host)}</code>\nWaktu: {now_str()}"
                        )
                        entry["last_alert_ts"] = now_ts
                    else:
                        print(f"[{now_str()}] (SUPPRESS by cooldown) {name} -> UP")
            else:
                entry["fails"] += 1
                entry["succ"] = 0
                if entry["fails"] >= self.fail_th and prev != "DOWN":
                    entry["status"] = "DOWN"
                    entry["last_down_ts"] = now_ts
                    changed = True
                    # Cooldown check
                    if now_ts - entry.get("last_alert_ts", 0) >= cooldown:
                        await self.broadcast(
                            f"❌ ALERT CCTV <b>{html.escape(name)}</b> DOWN\nHost: <code>{html.escape(host)}</code>\nWaktu: {now_str()}"
                        )
                        entry["last_alert_ts"] = now_ts
                    else:
                        print(f"[{now_str()}] (SUPPRESS by cooldown) {name} -> DOWN (fails={entry['fails']})")

        if changed:
            save_json(STATE_FILE, self.state)

    # -------- Daily heartbeat --------
    async def daily_heartbeat(self, context: ContextTypes.DEFAULT_TYPE):
        """Kirim pesan harian bahwa bot aktif + ringkasan status kamera."""
        if not self.subscribers:
            return
        # rangkum status
        up, down, unknown = [], [], []
        for cam in self.cameras:
            st = self.state.get(cam["name"], {}).get("status", "UNKNOWN")
            if st == "UP":
                up.append(cam["name"])
            elif st == "DOWN":
                down.append(cam["name"])
            else:
                unknown.append(cam["name"])
        msg_lines = [
            f"🫀 <b>Daily Heartbeat</b> — {now_str()}",
            f"Bot aktif. Total kamera: {len(self.cameras)}"
        ]
        if down:
            msg_lines.append(f"❌ DOWN: {', '.join(down)}")
        if unknown:
            msg_lines.append(f"❔ UNKNOWN: {', '.join(unknown)}")
        if not down and not unknown:
            msg_lines.append("✅ Semua kamera UP.")
        await self.broadcast("\n".join(msg_lines))

    # -------- Broadcast --------
    async def _send_with_retry(self, chat_id: int, text: str, tries: int = 3, **kwargs):
        backoff = 2
        for i in range(tries):
            try:
                return await self.app.bot.send_message(
                    chat_id, text, parse_mode=ParseMode.HTML, **kwargs
                )
            except (NetworkError, TimedOut):
                if i == tries - 1: raise
                await asyncio.sleep(backoff); backoff *= 2
            except Exception:
                if i == tries - 1: raise
                await asyncio.sleep(backoff); backoff *= 2

    async def broadcast(self, text: str):
        dead = []
        for chat_id in list(self.subscribers):
            try:
                await self._send_with_retry(chat_id, f"🛰️ {self.sender}\n\n{text}")
            except Exception as e:
                print(f"[{now_str()}] Kirim gagal ke {chat_id}: {e}")
                dead.append(chat_id)
        for d in dead:
            self.subscribers.discard(d)
        if dead:
            self._save_subs()

    # -------- Run --------
    def run(self):
        print(f"[{now_str()}] Bot jalan… interval {self.poll}s")
        self.app.run_polling()

if __name__ == "__main__":
    BotApp().run()
