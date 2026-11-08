import asyncio, os, sys, time, json, shutil, zipfile, subprocess, re, signal, threading

# Telegram native button colours require Bot API 9.6+ and an aiogram release
# that supports InlineKeyboardButton.style. Keep startup safe on Termux by
# upgrading aiogram automatically when an older release is installed.
def _ensure_button_style_support():
    try:
        from importlib.metadata import version
        installed_raw = version("aiogram")
        nums = tuple(int(x) for x in re.findall(r"\d+", installed_raw)[:3])
        installed = nums + (0,) * (3 - len(nums))
        if installed < (3, 27, 0):
            subprocess.check_call([
                sys.executable, "-m", "pip", "install", "-U",
                "aiogram>=3.27.0,<4"
            ])
            os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as exc:
        # If aiogram is missing, let the normal import below report the
        # dependency problem; do not hide unrelated runtime errors.
        if "No package metadata was found for aiogram" not in str(exc):
            print(f"[aiogram] style support check: {exc}")

_ensure_button_style_support()

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton, FSInputFile)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage


try:
    import resource
except ImportError:
    resource = None

def cpu_percent_fallback(interval=0.5):
    try:
        if hasattr(os, "getloadavg"):
            load = os.getloadavg()[0]
            cpu_count = os.cpu_count() or 1
            return round(min(100.0, max(0.0, load / cpu_count * 100)), 1)
    except Exception:
        pass
    return 0.0

def memory_percent_fallback():
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        avail = os.sysconf("SC_AVPHYS_PAGES")
        return round((1 - (avail / pages)) * 100, 1) if pages else 0.0
    except Exception:
        return 0.0

# ================= CONFIG =================
# Set BOT_TOKEN and OWNER_ID in the environment before starting.
BOT_TOKEN = os.getenv("BOT_TOKEN") or "8838209270:AAGMFJ8SvwGv8PwFx7owooebqUGcEZUR9z4"
OWNER_ID = 8154859186
OWNER_USERNAME = "Galu_Modz_Owner"
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "1") or 1)
PREMIUM_LIMIT = int(os.getenv("PREMIUM_LIMIT", "10") or 10)
BASE = os.path.dirname(os.path.abspath(__file__))
UPLOADS = os.path.join(BASE, "uploads")
DB_FILE = os.path.join(BASE, "db.json")
TMP_UPDATES = os.path.join(BASE, "tmp_updates")
# False = approved project update ke baad bhi approved rehta hai (owner ko updated file copy milti hai).
# True  = premium user ka update dobara Owner approval maangega.
REAPPROVE_ON_UPDATE = False
os.makedirs(UPLOADS, exist_ok=True)
os.makedirs(TMP_UPDATES, exist_ok=True)
START_TIME = time.time()
# ==========================================


def normalize_channel(value):
    value = (value or "").strip()
    if value.startswith("@"):
        return "https://t.me/" + value[1:]
    if value.startswith("https://t.me/") or value.startswith("http://t.me/"):
        return value
    if re.fullmatch(r"[A-Za-z0-9_]{5,}", value):
        return "https://t.me/" + value
    return value

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

processes = {}  # (uid, project) -> Popen


# ---------------- DB ----------------
def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"users": {}}


_db_lock = threading.RLock()


def save_db():
    """Atomic save: crash/restart beech me ho to db.json corrupt nahi hoga."""
    with _db_lock:
        data = None
        for _ in range(5):
            try:
                data = json.dumps(DB, indent=2)
                break
            except RuntimeError:       # dusre thread ne dict badla
                time.sleep(0.05)
        if data is None:
            return
        tmp = DB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, DB_FILE)


DB = load_db()
DB.setdefault("settings", {})
DB["settings"].setdefault("updates_channel", "")
save_db()

def get_updates_channel():
    return normalize_channel(DB.get("settings", {}).get("updates_channel", ""))

def set_updates_channel(value):
    DB.setdefault("settings", {})["updates_channel"] = normalize_channel(value)
    save_db()
    return get_updates_channel()


# ---------------- Hosting approval system ----------------
# Every hosted project must be approved by the Owner/Admin before it can run.
# Status is persisted in db.json so a restart cannot bypass the approval gate.
APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"


def project_key(uid, name):
    return f"{int(uid)}:{os.path.basename(name)}"


def get_approval(uid, name):
    approvals = DB.setdefault("approvals", {})
    rec = approvals.get(project_key(uid, name))
    if isinstance(rec, dict):
        return rec.get("status", APPROVAL_PENDING)
    # New/legacy projects are treated as pending for safety.
    return APPROVAL_PENDING


def set_approval(uid, name, status, admin_id=None):
    key = project_key(uid, name)
    DB.setdefault("approvals", {})[key] = {
        "status": status,
        "updated": time.time(),
        "admin_id": int(admin_id) if admin_id else OWNER_ID,
    }
    save_db()


def approval_text(status):
    return {
        APPROVAL_PENDING: "🟡 Pending Admin Approval",
        APPROVAL_APPROVED: "🟢 Approved",
        APPROVAL_REJECTED: "🔴 Rejected",
    }.get(status, "🟡 Pending Admin Approval")


def approval_kb(uid, name):
    key = project_key(uid, name)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            make_button("✅ Accept", callback_data=f"approve:{uid}:{name}"),
            make_button("❌ Reject", callback_data=f"reject:{uid}:{name}", style=DANGER_STYLE),
        ]
    ])



def get_user(u):
    uid = str(u.id)
    if uid not in DB["users"]:
        DB["users"][uid] = {"name": u.full_name, "username": u.username or "N/A",
                            "premium": False, "premium_until": 0,
                            "banned": False, "joined": time.time(), "_id": u.id}
        save_db()
    else:
        DB["users"][uid]["name"] = u.full_name
        DB["users"][uid]["username"] = u.username or "N/A"
        DB["users"][uid]["_id"] = u.id
        DB["users"][uid].setdefault("premium_until", 0)
        DB["users"][uid].setdefault("banned", False)
    return DB["users"][uid]


def user_dir(uid):
    p = os.path.join(UPLOADS, str(uid))
    os.makedirs(p, exist_ok=True)
    return p


def list_projects(uid):
    d = user_dir(uid)
    return sorted([x for x in os.listdir(d) if os.path.isdir(os.path.join(d, x))])


def limit_of(user):
    if int(user.get("_id", 0)) == OWNER_ID:
        return 10**9
    until = float(user.get("premium_until", 0) or 0)
    if user.get("premium") and until and time.time() >= until:
        user["premium"] = False
        user["premium_until"] = 0
        save_db()
    return PREMIUM_LIMIT if user.get("premium") else FREE_LIMIT

def is_owner(uid):
    return int(uid) == OWNER_ID

def can_update_files(uid):
    """Update Files sirf Owner aur active Premium users ke liye."""
    if is_owner(uid):
        return True
    user = DB["users"].get(str(uid))
    if not user:
        return False
    limit_of(user)          # premium expire ho gaya ho to yahin reset ho jayega
    return bool(user.get("premium"))

def bot_is_off():
    return bool(DB.get("settings", {}).get("bot_off", False))

def can_use_bot(uid):
    if is_owner(uid):
        return True
    user = DB["users"].get(str(uid))
    return bool(user) and not user.get("banned") and not bot_is_off()

def stop_all_user_projects(uid):
    for name in list_projects(uid):
        stop_project(uid, name, keep_desired=True)


def is_running(uid, name):
    p = processes.get((uid, name))
    return p is not None and p.poll() is None


# ---------------- UI / COLORS ----------------
# Telegram supports three native button styles: success (green),
# danger (red), and primary (blue). The helper below keeps the bot
# compatible with older aiogram versions: if an old aiogram is installed,
# it falls back to a normal button instead of crashing.
PRIMARY_STYLE = "success"   # GREEN
DANGER_STYLE = "danger"     # RED
NEUTRAL_STYLE = PRIMARY_STYLE  # GREEN primary as requested
PRIMARY_COLOR = "🟢"
DANGER_COLOR = "🔴"

def make_button(text, callback_data=None, url=None, style=PRIMARY_STYLE):
    """Create a Telegram button with a native Bot API colour style."""
    kwargs = {"text": text, "style": style}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    return InlineKeyboardButton(**kwargs)

def primary_text(text):
    return f"{PRIMARY_COLOR} {text}"

def danger_text(text):
    return f"{DANGER_COLOR} {text}"

# ---------------- UI ----------------
def main_kb():
    rows = []
    channel = get_updates_channel()
    if channel:
        rows.append([make_button(text="📢 Updates Channel", url=channel)])
    rows.extend([
        [make_button(text="📤 Upload File", callback_data="upload"),
         make_button(text="📂 Check Files", callback_data="files")],
        [make_button(text="⚡ Bot Speed", callback_data="speed"),
         make_button(text="📊 Statistics", callback_data="stats")],
        [make_button(text="💎 Premium", callback_data="premium")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [make_button(text="🔙 Back", callback_data="home", style=PRIMARY_STYLE)]])


LINE = "━━━━━━━━━━━━━━━━━━━━"


def welcome_text(u, user):
    rank = "💎 Premium" if user["premium"] else "🆓 Free User"
    if u.id == OWNER_ID:
        rank = "👑 Owner"
    return (f"👋 Hey — <b>{u.full_name}</b>, welcome back!\n{LINE}\n"
            f"🆔 <b>ID:</b> <code>{u.id}</code>\n"
            f"✳️ <b>Username:</b> @{u.username or 'N/A'}\n"
            f"🏖 <b>Rank:</b> {rank}\n"
            f"📁 <b>Projects:</b> {len(list_projects(u.id))} / {limit_of(user)}\n{LINE}\n\n"
            f"⚡ <b>What can I do for you?</b>\n"
            f"› Upload <code>.py</code> / <code>.js</code> scripts or <code>.zip</code> archives\n"
            f"› Run &amp; manage your bots 24/7\n"
            f"› Auto-install missing packages\n\n"
            f"👇 <b>Tap a button below to get started!</b>")


class St(StatesGroup):
    waiting_file = State()
    waiting_pkg = State()
    waiting_update = State()


# ---------------- Helpers ----------------
def find_entry(path):
    for n in ("main.py", "bot.py", "app.py", "index.js", "main.js", "bot.js", "app.js"):
        if os.path.exists(os.path.join(path, n)):
            return n
    for n in sorted(os.listdir(path)):
        if n.endswith((".py", ".js")):
            return n
    return None


def install_reqs(path):
    log = ""
    req = os.path.join(path, "requirements.txt")
    if os.path.exists(req):
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-r", req],
                           capture_output=True, text=True, timeout=600)
        log += r.stdout[-500:] + r.stderr[-500:]
    pkg = os.path.join(path, "package.json")
    if os.path.exists(pkg) and shutil.which("npm"):
        r = subprocess.run(["npm", "install"], cwd=path, capture_output=True,
                           text=True, timeout=600)
        log += r.stdout[-300:]
    return log


# ---------------- Process manager (auto-run + auto-restart) ----------------
MAX_FAILS = 8          # itni baar jaldi crash hua to auto-restart band
STABLE_SECS = 120      # itni der chala to crash counter reset
crash_info = {}        # "uid:name" -> restart bookkeeping
updating = set()       # update chalte waqt supervisor us project ko na chhede
PKG_ALIAS = {"cv2": "opencv-python", "PIL": "pillow", "yaml": "pyyaml",
             "bs4": "beautifulsoup4", "telebot": "pyTelegramBotAPI",
             "dotenv": "python-dotenv", "sklearn": "scikit-learn",
             "Crypto": "pycryptodome", "telegram": "python-telegram-bot"}


def set_desired(uid, name, on=True):
    """DB me yaad rakho ki ye project chalta rehna chahiye (host restart ke baad bhi)."""
    key = f"{uid}:{name}"
    ar = DB.setdefault("autorun", {})
    if on and not ar.get(key):
        ar[key] = True
        save_db()
    elif not on and key in ar:
        ar.pop(key, None)
        save_db()


def _kill_pid(pid, entry=None):
    """Purana (orphan) process band karo taaki same token/.db par 2 copy na chalein."""
    try:
        pid = int(pid)
        if pid <= 1 or pid == os.getpid():
            return
        cp = f"/proc/{pid}/cmdline"
        if os.path.exists(cp) and entry:
            try:
                cmd = open(cp, "rb").read().decode(errors="ignore")
            except Exception:
                cmd = ""
            if entry not in cmd:      # PID reuse safety
                return
        try:
            os.killpg(pid, signal.SIGTERM) if hasattr(os, "killpg") else os.kill(pid, signal.SIGTERM)
        except Exception:
            os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except OSError:
                return
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


def _trim_log(logp, limit=2 * 1024 * 1024, keep=200 * 1024):
    try:
        if os.path.getsize(logp) > limit:
            with open(logp, "rb") as f:
                f.seek(-keep, os.SEEK_END)
                tail = f.read()
            with open(logp, "wb") as f:
                f.write(tail)
    except Exception:
        pass


def _uses_host_token(path):
    """True agar project ki files me HOST bot ka token mila (same token = dono bot conflict)."""
    tok = BOT_TOKEN
    exts = (".py", ".js", ".json", ".env", ".txt", ".cfg", ".ini", ".yml", ".yaml", ".toml")
    n = 0
    for root, dirs, files_ in os.walk(path):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "__pycache__", "venv", ".venv")]
        for fn in files_:
            if not (fn.lower().endswith(exts) or fn == ".env"):
                continue
            n += 1
            if n > 300:
                return False
            fp = os.path.join(root, fn)
            try:
                if os.path.getsize(fp) > 2 * 1024 * 1024:
                    continue
                with open(fp, "r", errors="ignore") as f:
                    if tok in f.read():
                        return True
            except Exception:
                pass
    return False


def start_project(uid, name):
    if not can_use_bot(uid):
        return False, "Admin ne bot OFF kiya hai ya aap banned ho."
    name = os.path.basename(name)
    status = get_approval(uid, name)
    if status != APPROVAL_APPROVED:
        if status == APPROVAL_REJECTED:
            return False, "Admin ne is bot request ko Reject kiya hai. Dobara approval ke bina run nahi hoga."
        return False, "Admin approval pending hai. Accept hone ke baad hi bot run hoga."
    path = os.path.join(user_dir(uid), name)
    if not os.path.isdir(path):
        return False, "Project nahi mila."
    entry = find_entry(path)
    if not entry:
        return False, "Koi .py/.js file nahi mili."
    if is_running(uid, name):
        return False, "Already running hai."
    if entry.endswith(".js") and not shutil.which("node"):
        return False, "Server pe Node.js installed nahi hai."
    if _uses_host_token(path):
        return False, ("CONFLICT: Is project me HOST bot ka hi token hai. Ek token par 2 bot "
                       "nahi chal sakte. BotFather se naya token bana ke apne bot me daalo.")

    pidf = os.path.join(path, ".run.pid")
    if os.path.exists(pidf):
        try:
            _kill_pid(open(pidf).read().strip(), entry)
        except Exception:
            pass

    logp = os.path.join(path, "run.log")
    _trim_log(logp)
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    for k in ("BOT_TOKEN", "OWNER_ID"):    # host ke secrets hosted bot ko na milein
        env.pop(k, None)
    cmd = [sys.executable, "-u", entry] if entry.endswith(".py") else ["node", entry]
    logf = open(logp, "ab")
    try:
        p = subprocess.Popen(cmd, cwd=path, stdout=logf, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, env=env, start_new_session=True)
    except Exception as e:
        return False, f"Start error: {e}"
    finally:
        logf.close()
    processes[(uid, name)] = p
    try:
        with open(pidf, "w") as f:
            f.write(str(p.pid))
    except Exception:
        pass
    set_desired(uid, name, True)
    return True, entry


def stop_project(uid, name, keep_desired=False):
    """keep_desired=True: sirf process roko (ban / bot OFF), auto-run yaad rakho."""
    if not keep_desired:
        set_desired(uid, name, False)
    p = processes.get((uid, name))
    stopped = False
    if p and p.poll() is None:
        def _sig(sg):
            try:
                if hasattr(os, "killpg"):
                    os.killpg(p.pid, sg)
                else:
                    p.terminate() if sg == signal.SIGTERM else p.kill()
            except Exception:
                try:
                    p.terminate() if sg == signal.SIGTERM else p.kill()
                except Exception:
                    pass
        _sig(signal.SIGTERM)
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _sig(getattr(signal, "SIGKILL", signal.SIGTERM))
            try:
                p.wait(timeout=2)
            except Exception:
                pass
        stopped = True
    try:
        os.remove(os.path.join(user_dir(uid), os.path.basename(name), ".run.pid"))
    except Exception:
        pass
    return stopped


def _missing_module(uid, name):
    try:
        with open(os.path.join(user_dir(uid), name, "run.log"), "r", errors="ignore") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4000))
            data = f.read()
    except Exception:
        return None
    m = re.findall(r"No module named ['\"]([\w\.\-]+)['\"]", data)
    return m[-1].split(".")[0] if m else None


async def _notify(uid, text):
    try:
        await bot.send_message(uid, text)
    except Exception:
        pass


def enforce_approval_on_startup():
    """Kill orphaned hosted processes that are not admin-approved.
    This closes the restart/reboot loophole: a pending/rejected project cannot
    keep running just because its child process survived a host restart.
    """
    if not os.path.isdir(UPLOADS):
        return
    for uid_s in os.listdir(UPLOADS):
        user_path = os.path.join(UPLOADS, uid_s)
        if not os.path.isdir(user_path):
            continue
        try:
            uid = int(uid_s)
        except ValueError:
            continue
        for name in os.listdir(user_path):
            project_path = os.path.join(user_path, name)
            if not os.path.isdir(project_path):
                continue
            if get_approval(uid, name) == APPROVAL_APPROVED:
                continue
            pidf = os.path.join(project_path, ".run.pid")
            if os.path.exists(pidf):
                try:
                    pid = open(pidf, "r", encoding="utf-8").read().strip()
                    entry = find_entry(project_path)
                    _kill_pid(pid, entry)
                except Exception:
                    pass
                try:
                    os.remove(pidf)
                except OSError:
                    pass
            set_desired(uid, name, False)


async def supervisor():
    """Har 5 sec check: jo bots 'chalte rehne chahiye' unko start / crash par restart karo.
    Host restart ke baad bhi saare bots apne aap wapas chalu ho jaate hain."""
    loop = asyncio.get_running_loop()
    await asyncio.sleep(2)
    while True:
        try:
            for key in list(DB.get("autorun", {}).keys()):
                try:
                    uid_s, name = key.split(":", 1)
                    uid = int(uid_s)
                except Exception:
                    continue
                if key in updating:
                    continue                     # Update Files chal raha hai
                if not os.path.isdir(os.path.join(UPLOADS, uid_s, name)):
                    set_desired(uid, name, False)
                    crash_info.pop(key, None)
                    continue
                approval = get_approval(uid, name)
                if approval != APPROVAL_APPROVED:
                    # Never auto-start a project that has not been accepted.
                    # Remove the autorun flag so it cannot bypass approval later.
                    set_desired(uid, name, False)
                    crash_info.pop(key, None)
                    if is_running(uid, name):
                        stop_project(uid, name, keep_desired=True)
                    continue
                if not can_use_bot(uid):
                    continue                     # banned / bot OFF: ON hote hi wapas chalega
                info = crash_info.setdefault(
                    key, {"fails": 0, "next": 0, "start": 0, "pkgs": set(), "seen": None})
                p = processes.get((uid, name))
                if p is not None and p.poll() is None:
                    if time.time() - info["start"] > STABLE_SECS:
                        info["fails"] = 0
                    continue
                # process band hai
                if p is not None and info["seen"] is not p:
                    info["seen"] = p
                    if time.time() - info["start"] < STABLE_SECS:
                        info["fails"] += 1
                    mod = _missing_module(uid, name)
                    if mod and mod not in info["pkgs"]:
                        info["pkgs"].add(mod)
                        pkgname = PKG_ALIAS.get(mod, mod)
                        r = await loop.run_in_executor(None, lambda: subprocess.run(
                            [sys.executable, "-m", "pip", "install", pkgname],
                            capture_output=True, text=True, timeout=300))
                        if r.returncode == 0:
                            info["fails"] = max(0, info["fails"] - 1)
                            info["next"] = 0
                            await _notify(uid, f"📦 Missing package <code>{pkgname}</code> install kiya, "
                                               f"<b>{name}</b> dobara start ho raha hai.")
                    if info["fails"] > MAX_FAILS:
                        set_desired(uid, name, False)
                        await _notify(uid, danger_text(
                            f"<b>{name}</b> baar-baar crash ho raha hai, auto-restart band kiya. "
                            f"Logs check karo, code theek karke Run dabao."))
                        continue
                    if info["next"] == 0:
                        info["next"] = time.time() + min(5 * 2 ** max(0, info["fails"] - 1), 120)
                if time.time() < info["next"]:
                    continue
                ok, res = await loop.run_in_executor(None, start_project, uid, name)
                info["start"] = time.time()
                info["next"] = 0 if ok else time.time() + 60
                if not ok and str(res).startswith("CONFLICT"):
                    set_desired(uid, name, False)
                    await _notify(uid, danger_text(f"<b>{name}</b>: {res}"))
        except Exception as exc:
            print(f"[supervisor] {exc}")
        await asyncio.sleep(5)


# ---------------- Handlers ----------------
@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    user = get_user(m.from_user)
    if user["banned"]:
        return await m.answer(danger_text("Aap banned ho."))
    if bot_is_off() and m.from_user.id != OWNER_ID:
        return await m.answer(danger_text("Bot abhi Admin ke dwara OFF hai."))
    text = welcome_text(m.from_user, user)
    if m.from_user.id == OWNER_ID:
        text += "\n\n👑 <b>Owner:</b> <code>/admin</code>"
    await m.answer(text, reply_markup=main_kb())


@router.callback_query(F.data == "home")
async def home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    user = get_user(c.from_user)
    await c.message.edit_text(welcome_text(c.from_user, user), reply_markup=main_kb())
    await c.answer()


@router.callback_query(F.data == "upload")
async def upload(c: CallbackQuery, state: FSMContext):
    user = get_user(c.from_user)
    if not can_use_bot(c.from_user.id):
        return await c.answer(danger_text("Bot abhi OFF hai ya aap banned ho."), show_alert=True)
    if len(list_projects(c.from_user.id)) >= limit_of(user):
        return await c.answer(danger_text("Project limit full! Premium lo ya purana delete karo."),
                              show_alert=True)
    await state.set_state(St.waiting_file)
    await c.message.edit_text(
        "📤 <b>Upload File</b>\n\nAb apni <code>.py</code> / <code>.js</code> ya "
        "<code>.zip</code> file bhejo (max 20 MB).", reply_markup=back_kb())
    await c.answer()


@router.message(St.waiting_file, F.document)
async def got_file(m: Message, state: FSMContext):
    user = get_user(m.from_user)
    uid = m.from_user.id
    if not can_use_bot(uid):
        return await m.answer(danger_text("Bot abhi OFF hai ya aap banned ho."))
    doc = m.document
    fn = doc.file_name or "file"
    if not fn.lower().endswith((".py", ".js", ".zip")):
        return await m.answer(danger_text("Sirf .py / .js / .zip allowed hai."))
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        return await m.answer(danger_text("File 20MB se badi hai."))
    if len(list_projects(uid)) >= limit_of(user):
        return await m.answer(danger_text("Project limit full."))

    name = re.sub(r"[^\w\-]", "_", os.path.splitext(fn)[0])[:30] or "project"
    path = os.path.join(user_dir(uid), name)
    if os.path.exists(path):
        name += str(int(time.time()) % 1000)
        path = os.path.join(user_dir(uid), name)
    os.makedirs(path)
    tmp = os.path.join(path, fn)
    msg = await m.answer("⏳ Downloading...")
    await bot.download(doc, destination=tmp)

    if fn.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(tmp) as z:
                for member in z.namelist():  # zip-slip protection
                    target = os.path.realpath(os.path.join(path, member))
                    if not target.startswith(os.path.realpath(path)):
                        raise Exception("Unsafe zip")
                z.extractall(path)
            os.remove(tmp)
            items = os.listdir(path)  # single top folder ho to bahar nikalo
            if len(items) == 1 and os.path.isdir(os.path.join(path, items[0])):
                inner = os.path.join(path, items[0])
                for x in os.listdir(inner):
                    shutil.move(os.path.join(inner, x), path)
                os.rmdir(inner)
        except Exception as e:
            shutil.rmtree(path, ignore_errors=True)
            return await msg.edit_text(danger_text(f"Zip error: {e}"))

    await msg.edit_text("📦 Packages install ho rahe hain...")
    await asyncio.get_event_loop().run_in_executor(None, install_reqs, path)
    # IMPORTANT: upload never starts the hosted bot. It enters the admin approval queue.
    set_approval(uid, name, APPROVAL_PENDING, OWNER_ID)
    set_desired(uid, name, False)
    crash_info.pop(f"{uid}:{name}", None)
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [make_button(text="🟡 Waiting for Admin Approval", callback_data=f"proj:{name}")],
        [make_button(text="📂 Check Files", callback_data="files")]])
    warn = ""
    if _uses_host_token(path):
        warn = ("\n\n⚠️ <b>Warning:</b> Is file me HOST bot ka token hai. Ye run nahi hoga. "
                "Apne bot ka alag token (BotFather) daalo.")
    await msg.edit_text(f"✅ <b>{name}</b> upload ho gaya!\n🟡 <b>Admin approval required before this bot can run.</b>{warn}", reply_markup=kb)
    try:
        await bot.send_message(
            OWNER_ID,
            f"📥 <b>New File Hosted</b>\n"
            f"👤 User: {m.from_user.full_name} (<code>{uid}</code>)\n"
            f"📁 Project: <code>{name}</code>\n"
            f"📄 File: <code>{fn}</code>"
        )
        # Forward the uploaded source file to the owner.
        if os.path.exists(tmp):
            await bot.send_document(
                OWNER_ID,
                FSInputFile(tmp, filename=fn),
                caption=f"📦 Uploaded by {m.from_user.full_name} | ID: {uid}"
            )
        else:
            # ZIPs are extracted and the temporary ZIP is removed. Create a
            # clean owner copy of the extracted project and send it.
            owner_zip = os.path.join(path, f"{name}_owner_copy.zip")
            with zipfile.ZipFile(owner_zip, "w", zipfile.ZIP_DEFLATED) as z:
                for root, dirs, files_ in os.walk(path):
                    for file_ in files_:
                        full = os.path.join(root, file_)
                        if full == owner_zip or file_ == "run.log":
                            continue
                        z.write(full, os.path.relpath(full, path))
            await bot.send_document(
                OWNER_ID,
                FSInputFile(owner_zip, filename=os.path.basename(owner_zip)),
                caption=f"📦 Project copy | {name} | User ID: {uid}"
            )
            try:
                os.remove(owner_zip)
            except OSError:
                pass

        # Separate approval request with Accept/Reject controls.
        await bot.send_message(
            OWNER_ID,
            f"🔐 <b>BOT APPROVAL REQUEST</b>\n{LINE}\n"
            f"👤 User: <b>{m.from_user.full_name}</b>\n"
            f"🆔 User ID: <code>{uid}</code>\n"
            f"📁 Project: <code>{name}</code>\n"
            f"📄 File: <code>{fn}</code>\n\n"
            f"🟡 <b>This bot will NOT run until you accept it.</b>\n"
            f"Choose an action below:",
            reply_markup=approval_kb(uid, name)
        )
    except Exception as exc:
        # Hosting itself remains successful if the owner has not started the
        # bot or Telegram temporarily rejects the notification.
        print(f"[owner notification] {exc}")


# ---------------- Update Files (Premium + Owner only) ----------------
PROTECTED_FILES = {"run.log", ".run.pid"}


def _flatten_single_dir(path):
    items = [x for x in os.listdir(path) if x != "__MACOSX"]
    if len(items) == 1 and os.path.isdir(os.path.join(path, items[0])):
        inner = os.path.join(path, items[0])
        for x in os.listdir(inner):
            shutil.move(os.path.join(inner, x), path)
        os.rmdir(inner)


def _merge_tree(src, dst):
    """src ki saari files dst me copy/overwrite. dst ki baaki files (.db, data, config) safe rehti hain."""
    n = 0
    for root, dirs, files_ in os.walk(src):
        dirs[:] = [d for d in dirs if d != "__MACOSX"]
        rel = os.path.relpath(root, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_dir, exist_ok=True)
        for fn_ in files_:
            if rel == "." and fn_ in PROTECTED_FILES:
                continue
            shutil.copy2(os.path.join(root, fn_), os.path.join(target_dir, fn_))
            n += 1
    return n


def apply_update(uid, name, fn, tmp_file):
    """Blocking: uploaded file ko project me merge karo. Returns (files_count, note)."""
    path = os.path.join(user_dir(uid), name)
    stage = os.path.join(TMP_UPDATES, f"{uid}_{int(time.time() * 1000)}")
    os.makedirs(stage, exist_ok=True)
    try:
        if fn.lower().endswith(".zip"):
            real_stage = os.path.realpath(stage)
            with zipfile.ZipFile(tmp_file) as z:
                for member in z.namelist():           # zip-slip protection
                    target = os.path.realpath(os.path.join(stage, member))
                    if target != real_stage and not target.startswith(real_stage + os.sep):
                        raise Exception("Unsafe zip")
                z.extractall(stage)
            _flatten_single_dir(stage)
            if not any(True for _ in os.walk(stage)) or not os.listdir(stage):
                raise Exception("Zip khali hai")
            count = _merge_tree(stage, path)
            return count, "zip files merge ho gayi"
        # single .py / .js
        ext = os.path.splitext(fn)[1].lower()
        dest_name = fn
        note = "file replace ho gayi" if os.path.exists(os.path.join(path, fn)) else "nayi file add ho gayi"
        if not os.path.exists(os.path.join(path, fn)):
            scripts = [x for x in os.listdir(path)
                       if os.path.isfile(os.path.join(path, x)) and x.lower().endswith(ext)]
            if len(scripts) == 1:                      # project ki ek hi script = entry file
                dest_name = scripts[0]
                note = f"<code>{scripts[0]}</code> naye code se replace ho gayi"
        shutil.copy2(tmp_file, os.path.join(path, dest_name))
        return 1, note
    finally:
        shutil.rmtree(stage, ignore_errors=True)


@router.callback_query(F.data.startswith("upd:"))
async def update_files_start(c: CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    name = os.path.basename(c.data.split(":", 1)[1])
    get_user(c.from_user)
    if not can_update_files(uid):
        return await c.answer("💎 Update Files sirf Premium users aur Owner ke liye hai.", show_alert=True)
    if not can_use_bot(uid):
        return await c.answer("Bot abhi OFF hai ya aap banned ho.", show_alert=True)
    if not os.path.isdir(os.path.join(user_dir(uid), name)):
        return await c.answer("Project nahi mila.", show_alert=True)
    await state.set_state(St.waiting_update)
    await state.update_data(project=name)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [make_button(text="🔙 Cancel", callback_data=f"proj:{name}", style=DANGER_STYLE)]])
    await c.message.edit_text(
        f"♻️ <b>Update Files</b>\n{LINE}\n📁 Project: <code>{name}</code>\n\n"
        f"Ab naya <code>.py</code> / <code>.js</code> ya <code>.zip</code> bhejo (max 20 MB).\n\n"
        f"✅ Same bot token chalega — token apne code me wahi rakho.\n"
        f"✅ Purani files (database, data, config) delete nahi hongi, sirf same naam wali replace hongi.\n"
        f"✅ Agar bot chal raha tha to update ke baad automatically restart hoga.",
        reply_markup=kb)
    await c.answer()


@router.message(St.waiting_update, F.document)
async def update_files_receive(m: Message, state: FSMContext):
    uid = m.from_user.id
    user = get_user(m.from_user)
    data = await state.get_data()
    name = os.path.basename(data.get("project") or "")
    if not name:
        await state.clear()
        return await m.answer(danger_text("Session expire ho gaya. Check Files se dobara Update Files dabao."))
    if not can_update_files(uid):
        await state.clear()
        return await m.answer(danger_text("Update Files sirf Premium users aur Owner ke liye hai."))
    if not can_use_bot(uid):
        await state.clear()
        return await m.answer(danger_text("Bot abhi OFF hai ya aap banned ho."))
    path = os.path.join(user_dir(uid), name)
    if not os.path.isdir(path):
        await state.clear()
        return await m.answer(danger_text("Project nahi mila."))
    doc = m.document
    fn = os.path.basename(doc.file_name or "file")
    if not fn.lower().endswith((".py", ".js", ".zip")):
        return await m.answer(danger_text("Sirf .py / .js / .zip allowed hai. Sahi file bhejo."))
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        return await m.answer(danger_text("File 20MB se badi hai."))

    msg = await m.answer("⏳ Downloading update...")
    key = f"{uid}:{name}"
    tmp_file = os.path.join(TMP_UPDATES, f"{uid}_{int(time.time() * 1000)}_{fn}")
    loop = asyncio.get_running_loop()
    updating.add(key)
    try:
        await bot.download(doc, destination=tmp_file)
        was_running = is_running(uid, name)
        was_desired = bool(DB.get("autorun", {}).get(key))
        if was_running:
            await msg.edit_text("⏹ Bot stop ho raha hai...")
            await loop.run_in_executor(None, stop_project, uid, name, True)
        await msg.edit_text("♻️ Files update ho rahi hain...")
        count, note = await loop.run_in_executor(None, apply_update, uid, name, fn, tmp_file)
        await msg.edit_text("📦 Packages install ho rahe hain...")
        await loop.run_in_executor(None, install_reqs, path)
    except Exception as e:
        updating.discard(key)
        await state.clear()
        if os.path.exists(tmp_file):
            try: os.remove(tmp_file)
            except OSError: pass
        return await msg.edit_text(danger_text(f"Update error: {e}"),
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                       [make_button(text="🔙 Back", callback_data=f"proj:{name}")]]))
    await state.clear()
    crash_info.pop(key, None)
    updating.discard(key)

    # Owner ko updated file ki copy (owner khud update kare to nahi)
    if not is_owner(uid):
        try:
            await bot.send_message(
                OWNER_ID,
                f"♻️ <b>Files Updated</b>\n👤 User: {m.from_user.full_name} (<code>{uid}</code>)\n"
                f"📁 Project: <code>{name}</code>\n📄 File: <code>{fn}</code>")
            await bot.send_document(OWNER_ID, FSInputFile(tmp_file, filename=fn),
                                    caption=f"📦 Update by {m.from_user.full_name} | ID: {uid}")
        except Exception as exc:
            print(f"[update owner notification] {exc}")
    try:
        os.remove(tmp_file)
    except OSError:
        pass

    # Re-approval mode (optional)
    if REAPPROVE_ON_UPDATE and not is_owner(uid):
        set_approval(uid, name, APPROVAL_PENDING, OWNER_ID)
        set_desired(uid, name, False)
        try:
            await bot.send_message(
                OWNER_ID,
                f"🔐 <b>UPDATE APPROVAL REQUEST</b>\n{LINE}\n👤 User: <b>{m.from_user.full_name}</b>\n"
                f"🆔 User ID: <code>{uid}</code>\n📁 Project: <code>{name}</code>\n\n"
                f"🟡 Updated bot tab tak run nahi hoga jab tak accept na karo.",
                reply_markup=approval_kb(uid, name))
        except Exception as exc:
            print(f"[update approval request] {exc}")
        return await msg.edit_text(
            f"✅ <b>{name}</b> update ho gaya ({note}).\n🟡 Admin approval ke baad hi run hoga.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [make_button(text="📂 Check Files", callback_data="files")]]))

    # Auto start: pehle chal raha tha (ya autorun on tha) to wapas start
    status_line = "🔴 Bot stopped hai — Run dabao."
    if was_running or was_desired:
        ok, res = await loop.run_in_executor(None, start_project, uid, name)
        if ok:
            status_line = f"🟢 Bot restart ho gaya ({res})."
        else:
            status_line = danger_text(f"Bot start nahi hua: {res}")
    warn = ""
    if _uses_host_token(path):
        warn = ("\n\n⚠️ <b>Warning:</b> Is project me HOST bot ka token mila. Ye run nahi hoga. "
                "Apne bot ka alag token (BotFather) daalo.")
    await msg.edit_text(
        f"✅ <b>{name}</b> update ho gaya!\n{LINE}\n📝 {note}\n📄 Files: <code>{count}</code>\n{status_line}{warn}",
        reply_markup=proj_kb(name, is_running(uid, name), uid))


@router.message(St.waiting_update, ~F.document, ~F.text.startswith("/"))
async def update_files_wrong(m: Message):
    await m.answer(danger_text("Sirf .py / .js / .zip file bhejo, ya Cancel dabao."))


@router.callback_query(F.data == "files")
async def files(c: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = c.from_user.id
    projs = list_projects(uid)
    if not projs:
        return await c.message.edit_text("📂 Abhi koi project nahi hai.\n"
                                         "Pehle <b>Upload File</b> karo.",
                                         reply_markup=back_kb())
    rows = []
    for p in projs:
        approval = get_approval(uid, p)
        if approval == APPROVAL_APPROVED:
            s = PRIMARY_COLOR if is_running(uid, p) else DANGER_COLOR
        else:
            s = "🟡" if approval == APPROVAL_PENDING else DANGER_COLOR
        rows.append([make_button(text=f"{s} {p}", callback_data=f"proj:{p}")])
    rows.append([make_button(text="🔙 Back", callback_data="home", style=PRIMARY_STYLE)])
    await c.message.edit_text("📂 <b>Your Projects</b>",
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer()


def proj_kb(name, running, uid=None):
    first = (make_button(text="⏹ Stop", callback_data=f"stop:{name}", style=DANGER_STYLE) if running
             else make_button(text="▶️ Run", callback_data=f"run:{name}"))
    rows = [[first, make_button(text="🔄 Restart", callback_data=f"restart:{name}")]]
    if uid is not None and can_update_files(uid):
        rows.append([make_button(text="♻️ Update Files", callback_data=f"upd:{name}")])
    rows.append([make_button(text="📜 Logs", callback_data=f"logs:{name}"),
                 make_button(text="🗑 Delete", callback_data=f"del:{name}", style=DANGER_STYLE)])
    rows.append([make_button(text="🔙 Back", callback_data="files", style=PRIMARY_STYLE)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("proj:"))
async def proj(c: CallbackQuery, state: FSMContext):
    await state.clear()
    name = c.data.split(":", 1)[1]
    name = os.path.basename(name)
    approval = get_approval(c.from_user.id, name)
    run = is_running(c.from_user.id, name)
    status = approval_text(approval)
    if approval != APPROVAL_APPROVED:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [make_button("🔐 " + status, callback_data=f"proj:{name}")],
            [make_button("🔙 Back", callback_data="files", style=PRIMARY_STYLE)]
        ])
    else:
        kb = proj_kb(name, run, c.from_user.id)
    await c.message.edit_text(
        f"📁 <b>{name}</b>\nApproval: <b>{status}</b>\n"
        f"Status: {'🟢 Running' if run else '🔴 Stopped'}",
        reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("run:"))
async def run(c: CallbackQuery):
    name = c.data.split(":", 1)[1]
    uid = c.from_user.id
    crash_info.pop(f"{uid}:{name}", None)
    ok, info = await asyncio.get_running_loop().run_in_executor(None, start_project, uid, name)
    if not ok:
        return await c.answer(f"{danger_text(info)}", show_alert=True)
    await c.answer("🚀 Starting...")
    await c.message.edit_text(f"🚀 <b>{name}</b> start ho gaya ({info})",
                              reply_markup=proj_kb(name, True, c.from_user.id))


@router.callback_query(F.data.startswith("stop:"))
async def stop(c: CallbackQuery):
    name = c.data.split(":", 1)[1]
    crash_info.pop(f"{c.from_user.id}:{name}", None)
    await asyncio.get_running_loop().run_in_executor(None, stop_project, c.from_user.id, name)
    await c.message.edit_text(f"⏹ <b>{name}</b> stop ho gaya.",
                              reply_markup=proj_kb(name, False, c.from_user.id))
    await c.answer()


@router.callback_query(F.data.startswith("restart:"))
async def restart(c: CallbackQuery):
    name = c.data.split(":", 1)[1]
    crash_info.pop(f"{c.from_user.id}:{name}", None)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, stop_project, c.from_user.id, name)
    await asyncio.sleep(1)
    ok, info = await loop.run_in_executor(None, start_project, c.from_user.id, name)
    await c.answer("🔄 Restarted" if ok else f"{danger_text(info)}", show_alert=not ok)
    if ok:
        await c.message.edit_text(f"🔄 <b>{name}</b> restart ho gaya.",
                                  reply_markup=proj_kb(name, True, c.from_user.id))


@router.callback_query(F.data.startswith("logs:"))
async def logs(c: CallbackQuery):
    name = os.path.basename(c.data.split(":", 1)[1])
    lp = os.path.join(user_dir(c.from_user.id), name, "run.log")
    if not os.path.exists(lp) or os.path.getsize(lp) == 0:
        return await c.answer("Abhi koi log nahi hai.", show_alert=True)
    await c.message.answer_document(FSInputFile(lp, filename=f"{name}_log.txt"))
    await c.answer()


@router.callback_query(F.data.startswith("del:"))
async def delete(c: CallbackQuery):
    name = os.path.basename(c.data.split(":", 1)[1])
    crash_info.pop(f"{c.from_user.id}:{name}", None)
    await asyncio.get_running_loop().run_in_executor(None, stop_project, c.from_user.id, name)
    shutil.rmtree(os.path.join(user_dir(c.from_user.id), name), ignore_errors=True)
    processes.pop((c.from_user.id, name), None)
    await c.answer("🗑 Deleted", show_alert=True)
    await files(c, None) if False else await c.message.edit_text(
        "🗑 Project delete ho gaya.", reply_markup=back_kb())


@router.callback_query(F.data == "speed")
async def speed(c: CallbackQuery):
    t = time.time()
    await c.answer("Testing...")
    ms = round((time.time() - t) * 1000, 1)
    cpu = cpu_percent_fallback(0.5)
    ram = memory_percent_fallback()
    up = int(time.time() - START_TIME)
    await c.message.edit_text(
        f"⚡ <b>Bot Speed</b>\n{LINE}\n🏓 Ping: <code>{ms} ms</code>\n"
        f"🖥 CPU: <code>{cpu}%</code>\n💾 RAM: <code>{ram}%</code>\n"
        f"⏱ Uptime: <code>{up // 3600}h {(up % 3600) // 60}m</code>",
        reply_markup=back_kb())


@router.callback_query(F.data == "stats")
async def stats(c: CallbackQuery):
    total = len(DB["users"])
    prem = sum(1 for u in DB["users"].values() if u["premium"])
    running = sum(1 for k in processes if is_running(*k))
    await c.message.edit_text(
        f"📊 <b>Statistics</b>\n{LINE}\n👥 Total Users: <code>{total}</code>\n"
        f"💎 Premium: <code>{prem}</code>\n🟢 Running Bots: <code>{running}</code>",
        reply_markup=back_kb())
    await c.answer()


@router.callback_query(F.data == "premium")
async def premium(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [make_button(text="📞 Contact Owner",
                              url=f"https://t.me/{OWNER_USERNAME}")],
        [make_button(text="🔙 Back", callback_data="home", style=PRIMARY_STYLE)]])
    await c.message.edit_text(
        f"💎 <b>Premium</b>\n{LINE}\n✅ {PREMIUM_LIMIT} projects tak upload\n"
        f"✅ Priority support\n\nBuy karne ke liye owner se contact karo.",
        reply_markup=kb)
    await c.answer()


@router.callback_query(F.data == "manual")
async def manual(c: CallbackQuery, state: FSMContext):
    await state.set_state(St.waiting_pkg)
    await c.message.edit_text(
        "📦 <b>Manual Install</b>\n\nPackage ka naam bhejo (pip), jaise "
        "<code>requests</code> ya <code>aiohttp pillow</code>",
        reply_markup=back_kb())
    await c.answer()


@router.message(St.waiting_pkg, F.text)
async def do_install(m: Message, state: FSMContext):
    pkgs = m.text.split()
    if not all(re.fullmatch(r"[A-Za-z0-9_\-\.\[\]=<>]+", p) for p in pkgs):
        return await m.answer(danger_text("Invalid package name."))
    msg = await m.answer("⏳ Installing...")
    r = await asyncio.get_event_loop().run_in_executor(
        None, lambda: subprocess.run([sys.executable, "-m", "pip", "install", *pkgs],
                                     capture_output=True, text=True, timeout=300))
    await state.clear()
    ok = r.returncode == 0
    await msg.edit_text(("✅ Installed: " if ok else "❌ Failed:\n") +
                        (" ".join(pkgs) if ok else f"<code>{r.stderr[-400:]}</code>"),
                        reply_markup=back_kb())


@router.callback_query(F.data == "info")
async def info(c: CallbackQuery):
    user = get_user(c.from_user)
    await c.message.edit_text(
        f"👤 <b>My Info</b>\n{LINE}\n🆔 ID: <code>{c.from_user.id}</code>\n"
        f"📛 Name: {c.from_user.full_name}\n✳️ Username: @{c.from_user.username or 'N/A'}\n"
        f"🏖 Rank: {'💎 Premium' if user['premium'] else '🆓 Free User'}\n"
        f"📁 Projects: {len(list_projects(c.from_user.id))} / {limit_of(user)}",
        reply_markup=back_kb())
    await c.answer()


# ---------------- Owner/Admin controls ----------------
def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [make_button("👥 All Users", callback_data="admin_users"), make_button("💎 Give Premium", callback_data="admin_premium")],
        [make_button("🔴 Ban User", callback_data="admin_ban", style=DANGER_STYLE), make_button("🟢 Unban User", callback_data="admin_unban")],
        [make_button("⛔ Bot OFF", callback_data="admin_off", style=DANGER_STYLE), make_button("▶️ Bot ON", callback_data="admin_on")],
        [make_button("🔄 Refresh", callback_data="admin_panel")]
    ])

@router.message(F.text == "/admin", F.from_user.id == OWNER_ID)
async def owner_admin(m: Message):
    total=len(DB["users"]); banned=sum(1 for u in DB["users"].values() if u.get("banned"))
    premium=sum(1 for u in DB["users"].values() if u.get("premium") and (not u.get("premium_until") or time.time()<u.get("premium_until")))
    await m.answer(f"👑 <b>Owner / Admin Panel</b>\n{LINE}\n👥 All Users: <code>{total}</code>\n💎 Premium Users: <code>{premium}</code>\n🚫 Banned Users: <code>{banned}</code>\n🤖 Bot: <b>{'🔴 OFF' if bot_is_off() else '🟢 ON'}</b>\n\n<code>/premium USER_ID DAYS</code>\n<code>/ban USER_ID</code> / <code>/unban USER_ID</code>\n<code>/botoff</code> / <code>/boton</code>", reply_markup=admin_kb())

@router.callback_query(F.data.startswith("approve:"))
async def approve_project(c: CallbackQuery):
    if c.from_user.id != OWNER_ID:
        return await c.answer("Owner only.", show_alert=True)
    parts = c.data.split(":", 2)
    if len(parts) != 3:
        return await c.answer("Invalid request.", show_alert=True)
    try:
        uid = int(parts[1])
    except ValueError:
        return await c.answer("Invalid user ID.", show_alert=True)
    name = os.path.basename(parts[2])
    path = os.path.join(user_dir(uid), name)
    if not os.path.isdir(path):
        return await c.answer("Project nahi mila.", show_alert=True)
    set_approval(uid, name, APPROVAL_APPROVED, OWNER_ID)
    # Approval enables running but does not silently start the bot.
    set_desired(uid, name, False)
    await c.message.edit_text(
        f"✅ <b>BOT APPROVED</b>\n{LINE}\n"
        f"👤 User ID: <code>{uid}</code>\n📁 Project: <code>{name}</code>\n\n"
        f"🟢 User ab is bot ko Run kar sakta hai. Bot upload ke time automatically start nahi hota."
    )
    await _notify(uid, f"✅ <b>{name}</b> ko Admin ne approve kar diya hai. Ab aap Run kar sakte ho.")
    await c.answer("Approved.")

@router.callback_query(F.data.startswith("reject:"))
async def reject_project(c: CallbackQuery):
    if c.from_user.id != OWNER_ID:
        return await c.answer("Owner only.", show_alert=True)
    parts = c.data.split(":", 2)
    if len(parts) != 3:
        return await c.answer("Invalid request.", show_alert=True)
    try:
        uid = int(parts[1])
    except ValueError:
        return await c.answer("Invalid user ID.", show_alert=True)
    name = os.path.basename(parts[2])
    path = os.path.join(user_dir(uid), name)
    if not os.path.isdir(path):
        return await c.answer("Project nahi mila.", show_alert=True)
    set_approval(uid, name, APPROVAL_REJECTED, OWNER_ID)
    set_desired(uid, name, False)
    crash_info.pop(f"{uid}:{name}", None)
    await asyncio.get_running_loop().run_in_executor(None, stop_project, uid, name)
    await c.message.edit_text(
        f"❌ <b>BOT REJECTED</b>\n{LINE}\n"
        f"👤 User ID: <code>{uid}</code>\n📁 Project: <code>{name}</code>\n\n"
        f"🔴 This bot is blocked and will not run."
    )
    await _notify(uid, f"❌ <b>{name}</b> ki hosting request Admin ne reject kar di hai. Bot run nahi hoga.")
    await c.answer("Rejected.")

@router.callback_query(F.data == "admin_panel")
async def admin_panel_cb(c: CallbackQuery):
    if c.from_user.id != OWNER_ID: return await c.answer("Owner only.", show_alert=True)
    await c.message.edit_text(f"👑 <b>Owner / Admin Panel</b>\n{LINE}\n👥 All Users: <code>{len(DB['users'])}</code>\n🚫 Banned: <code>{sum(1 for u in DB['users'].values() if u.get('banned'))}</code>\n🤖 Bot: <b>{'🔴 OFF' if bot_is_off() else '🟢 ON'}</b>", reply_markup=admin_kb())
    await c.answer()

@router.callback_query(F.data == "admin_users")
async def admin_users_cb(c: CallbackQuery):
    if c.from_user.id != OWNER_ID: return await c.answer("Owner only.", show_alert=True)
    lines=["👥 <b>All Registered Users</b>",LINE]
    for i,(uid,u) in enumerate(DB["users"].items(),1):
        status="🚫 BANNED" if u.get("banned") else ("💎 PREMIUM" if u.get("premium") else "🆓 FREE")
        lines.append(f"{i}. <b>{u.get('name','N/A')}</b> — <code>{uid}</code> — {status}")
        if i>=50: lines.append("…first 50 users shown."); break
    if len(lines)==2: lines.append("No users yet.")
    await c.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[make_button("🔙 Admin",callback_data="admin_panel")]]))
    await c.answer()

@router.callback_query(F.data == "admin_off")
async def admin_off_cb(c: CallbackQuery):
    if c.from_user.id != OWNER_ID: return await c.answer("Owner only.", show_alert=True)
    DB.setdefault("settings",{})["bot_off"]=True; save_db()
    for uid in DB["users"]:
        if int(uid)!=OWNER_ID: stop_all_user_projects(int(uid))
    await c.answer("Bot OFF — user bots stopped.", show_alert=True); await admin_panel_cb(c)

@router.callback_query(F.data == "admin_on")
async def admin_on_cb(c: CallbackQuery):
    if c.from_user.id != OWNER_ID: return await c.answer("Owner only.", show_alert=True)
    DB.setdefault("settings",{})["bot_off"]=False; save_db()
    await c.answer("Bot ON.", show_alert=True); await admin_panel_cb(c)

@router.callback_query(F.data.in_({"admin_ban","admin_unban","admin_premium"}))
async def admin_action_help(c: CallbackQuery):
    if c.from_user.id != OWNER_ID: return await c.answer("Owner only.", show_alert=True)
    cmd={"admin_ban":"/ban USER_ID","admin_unban":"/unban USER_ID","admin_premium":"/premium USER_ID DAYS"}[c.data]
    await c.message.edit_text(f"🛠 <b>Admin Action</b>\n{LINE}\nSend: <code>{cmd}</code>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[make_button("🔙 Admin",callback_data="admin_panel")]]))
    await c.answer()

@router.message(F.text.startswith("/setchannel"), F.from_user.id == OWNER_ID)
async def setchannel(m: Message):
    value = m.text.replace("/setchannel", "", 1).strip()
    if not value:
        return await m.answer(
            "Use: <code>/setchannel @yourchannel</code> or "
            "<code>/setchannel https://t.me/yourchannel</code>"
        )
    channel = set_updates_channel(value)
    await m.answer(primary_text(f"Updates channel set to: {channel}"))

@router.message(F.text == "/channel", F.from_user.id == OWNER_ID)
async def channel(m: Message):
    await m.answer(
        f"📢 Current Updates Channel:\n<code>{get_updates_channel() or 'Not set'}</code>"
    )


@router.message(F.text.startswith("/premium"), F.from_user.id == OWNER_ID)
async def give_premium(m: Message):
    try:
        p=m.text.split()
        if len(p)<3: return await m.answer("Use: /premium USER_ID DAYS")
        uid,days=str(int(p[1])),int(p[2])
        if uid not in DB["users"]: return await m.answer("User nahi mila.")
        if days<=0 or days>36500: return await m.answer("Days 1-36500 ke beech do.")
        DB["users"][uid]["premium"]=True
        DB["users"][uid]["premium_until"]=time.time()+days*86400
        save_db(); await m.answer(primary_text(f"{uid} ko {days} days premium de diya."))
    except Exception: await m.answer("Use: /premium user_id days")

@router.message(F.text.startswith("/ban"), F.from_user.id == OWNER_ID)
async def ban(m: Message):
    try:
        uid=str(int(m.text.split()[1]))
        if int(uid)==OWNER_ID: return await m.answer("Owner ko ban nahi kar sakte.")
        if uid not in DB["users"]: return await m.answer("User nahi mila.")
        DB["users"][uid]["banned"]=True; stop_all_user_projects(int(uid)); save_db()
        await m.answer(danger_text(f"{uid} banned aur uske bots stopped."))
    except Exception: await m.answer("Use: /ban user_id")

@router.message(F.text.startswith("/unban"), F.from_user.id == OWNER_ID)
async def unban(m: Message):
    try:
        uid=str(int(m.text.split()[1]))
        if uid not in DB["users"]: return await m.answer("User nahi mila.")
        DB["users"][uid]["banned"]=False; save_db()
        await m.answer(primary_text(f"{uid} unbanned."))
    except Exception: await m.answer("Use: /unban user_id")

@router.message(F.text == "/botoff", F.from_user.id == OWNER_ID)
async def botoff(m: Message):
    DB.setdefault("settings",{})["bot_off"]=True; save_db()
    for uid in DB["users"]:
        if int(uid)!=OWNER_ID: stop_all_user_projects(int(uid))
    await m.answer(danger_text("Bot OFF. User bots start nahi honge; owner unlimited run kar sakta hai."))

@router.message(F.text == "/boton", F.from_user.id == OWNER_ID)
async def boton(m: Message):
    DB.setdefault("settings",{})["bot_off"]=False; save_db()
    await m.answer(primary_text("Bot ON. Users dobara bots run kar sakte hain."))

@router.message(F.text.startswith("/broadcast"), F.from_user.id == OWNER_ID)
async def broadcast(m: Message):
    text = m.text.replace("/broadcast", "", 1).strip()
    if not text:
        return await m.answer("Use: /broadcast message")
    n = 0
    for uid in DB["users"]:
        try:
            await bot.send_message(int(uid), text)
            n += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await m.answer(f"📢 {n} users ko bheja.")


async def main():
    print("Bot started...")
    # Security gate first: no pending/rejected hosted project may survive a restart.
    enforce_approval_on_startup()
    asyncio.create_task(supervisor())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())