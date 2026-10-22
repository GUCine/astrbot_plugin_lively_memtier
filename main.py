"""機器人的記憶分層：日 → 周 → 月 → 季。

LivingMemory 每聊一段就寫一條記憶（一天幾十條），這裡每天 RUN_AT 把它們逐級壓縮：
- 日：前一天同一個群（或同一個人的私聊）的零碎記憶合成幾條「日記憶」，保留 7 天
- 周：超過 7 天的日記憶按自然周合成「週記憶」，保留 4 周
- 月：超過 4 周的週記憶按月合成「月記憶」，保留 3 個月
- 季：超過 3 個月的月記憶按季度合成「季記憶」，長期保留
同一時段後來又冒出低一級的記憶（比如晚到的一段聊天），會和已有的那條重新合併
重要度 >= PROTECT 的記憶（生日、約定、主人交代的事）原樣保留，不參與壓縮
被合併的舊記憶只是歸檔（不再參與召回），文檔還在，可以恢復
只通過 LivingMemory 自己的 add_memory / archive_memories 寫入，不改它的程式碼
主人私信指令：記憶分層 狀態 / 現在 / 開 / 關
"""
import asyncio
import json
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api import AstrBotConfig
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.message_event_result import MessageChain

from .lively_common import Settings, T2S

LM_PLUGIN = "astrbot_plugin_livingmemory"
TZ = timezone(timedelta(hours=8))
RUN_AT = "03:30"  # 在自我成長 04:30 覆盤之前
PROTECT = 0.8
KEEP_DAYS, KEEP_WEEKS, KEEP_MONTHS = 7, 4, 3
MAX_CALLS = 40  # 每次最多呼叫模型這麼多次，剩下的明天接著壓
MAX_PEOPLE = 8  # LivingMemory 圖譜每條記憶最多連 8 個人
TIERS = ("day", "week", "month", "quarter")
TIER_NAMES = {"day": "日记忆", "week": "周记忆", "month": "月记忆", "quarter": "季记忆"}
MAX_ENTRIES = {"day": 6, "week": 5, "month": 4, "quarter": 3}  # 上限；實際按條數定，10 條左右合成 1 條
BATCH = 45  # 一次最多餵給模型這麼多條，再多就按時間分批
MAX_CHARS = {"day": 300, "week": 400, "month": 500, "quarter": 600}

MERGE_RULE = """你是〈名字〉（自称「〈我〉」或「我」），在整理自己的回忆：把下面同一个{where}在{period}的零碎记忆，压缩成不超过 {n} 条{tier}。
要求：
- 按事情或话题分条，同一件事的不同片段合成一条；每条的 summary 用第一人称写成〈我〉自己的回忆，不超过 {chars} 字
- 保留具体的人名、说过的话、约定、喜好、梗和数字，去掉重复和寒暄；不要编没给你的事
- key_facts：每条最多 6 个以后可能用得上的具体事实（谁、做了/说了什么）
- qq：这件事主要牵涉的人的 QQ 号，最多 {people} 个，从「人物」里选，〈我〉自己不用写
- importance：0～1，越可能以后被提起越高
只输出 JSON：{{"entries": [{{"summary": "…", "key_facts": ["…"], "topics": ["…"], "qq": ["…"], "importance": 0.5}}]}}"""


def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def _meta(raw) -> dict:
    try:
        m = json.loads(raw or "{}")
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def _when(m: dict) -> datetime:
    """一條記憶屬於哪個時間點：分層記憶用時段結束，原始記憶用對話結束時間"""
    if m.get("lively_period_end"):
        return datetime.fromtimestamp(float(m["lively_period_end"]), TZ)
    s = m.get("source_time_end") or m.get("source_time_start")
    if s:
        try:
            return datetime.fromisoformat(str(s)).astimezone(TZ)
        except Exception:
            pass
    return datetime.fromtimestamp(float(m.get("create_time") or 0), TZ)


def _period(tier: str, d: datetime) -> tuple[str, datetime, datetime, str]:
    """(時段鍵, 開始, 結束, 給人看的標籤)"""
    day = d.replace(hour=0, minute=0, second=0, microsecond=0)
    if tier == "day":
        return day.strftime("%Y-%m-%d"), day, day + timedelta(days=1), day.strftime("%Y-%m-%d")
    if tier == "week":
        start = day - timedelta(days=day.weekday())
        end = start + timedelta(days=7)
        y, w, _ = start.isocalendar()
        return f"{y}-W{w:02d}", start, end, f"{start:%m-%d}～{end - timedelta(days=1):%m-%d}（第{w}周）"
    if tier == "month":
        # 跨月的周按週四所在的月算（ISO 周的規矩）
        start = day.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
        return start.strftime("%Y-%m"), start, end, f"{start.year}年{start.month}月"
    q = (day.month - 1) // 3
    start = day.replace(month=q * 3 + 1, day=1)
    end = (start + timedelta(days=100)).replace(day=1)
    return f"{start.year}-Q{q + 1}", start, end, f"{start.year}年第{q + 1}季度"


@register(
    "astrbot_plugin_lively_memtier",
    "GUCine",
    "給 LivingMemory 做記憶分層：零碎記憶按日、周、月、季逐級壓縮，省 token",
    "2.0.0",
)
class LivelyMemtier(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.s = Settings(context, config)
        self.dir = Path(StarTools.get_data_dir("astrbot_plugin_lively_memtier"))
        self.state_file = self.dir / "state.json"
        self.events_file = self.dir / "events.jsonl"
        try:
            self.state = json.loads(self.state_file.read_text("utf-8"))
        except Exception:
            self.state = {}
        self.state.setdefault("enabled", True)
        self.state.setdefault("last_day", "")
        self.state.setdefault("last_result", "")
        self._busy = asyncio.Lock()
        self._task = asyncio.create_task(self._loop())

    async def terminate(self):
        self._task.cancel()

    def _save(self):
        try:
            self.state_file.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), "utf-8")
        except Exception as e:
            logger.error(f"[lively_memtier] 保存状态失败: {e}")

    def _log(self, **data):
        try:
            with self.events_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": int(time.time()), **data}, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _engine(self):
        star = self.context.get_registered_star(LM_PLUGIN)
        lm = getattr(star, "star_cls", None)
        eng = getattr(getattr(lm, "initializer", None), "memory_engine", None)
        if eng is None or getattr(eng, "db_connection", None) is None:
            raise RuntimeError("LivingMemory 还没准备好")
        return eng

    async def _loop(self):
        await asyncio.sleep(180)  # 等 LivingMemory 初始化完
        while True:
            try:
                now = datetime.now(TZ)
                day = now.strftime("%Y-%m-%d")
                if self.state["enabled"] and now.strftime("%H:%M") >= RUN_AT and self.state["last_day"] != day:
                    self.state["last_day"] = day
                    self._save()
                    await self.run()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[lively_memtier] 定时任务出错: {e}", exc_info=True)
            await asyncio.sleep(60)

    # ---------- 讀記憶 ----------
    async def _active(self, eng) -> list[dict]:
        cur = await eng.db_connection.execute(
            "SELECT id, text, metadata FROM documents "
            "WHERE COALESCE(json_extract(metadata, '$.status'), 'active') = 'active' ORDER BY id")
        out = []
        for row in await cur.fetchall():
            m = _meta(row["metadata"])
            scope = m.get("session_id") or m.get("source_session_id")
            if not scope:
                continue
            out.append({"id": int(row["id"]), "text": row["text"], "meta": m, "scope": str(scope),
                        "tier": m.get("lively_tier") or "raw", "when": _when(m)})
        return out

    def _plan(self, docs: list[dict], now: datetime) -> list[tuple[str, str, datetime, list[dict]]]:
        """要合併的組：[(層級, 範圍, 時段裡的某個時間點, 記憶列表)]，同一組裡有低一級的記憶才需要動"""
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # 每一級：哪些低一級的記憶到期了，要升上來
        due = {
            "day": lambda d: d["tier"] == "raw" and d["when"] < today,
            "week": lambda d: d["tier"] == "day" and d["when"] < today - timedelta(days=KEEP_DAYS),
            "month": lambda d: d["tier"] == "week" and d["when"] < today - timedelta(weeks=KEEP_WEEKS),
            "quarter": lambda d: d["tier"] == "month" and d["when"] < (today - timedelta(days=31 * KEEP_MONTHS)),
        }
        plans = []
        for tier in TIERS:
            groups: dict[tuple[str, str], list[dict]] = {}
            reps: dict[tuple[str, str], datetime] = {}
            for d in docs:
                if float(d["meta"].get("importance") or 0) >= PROTECT and d["tier"] == "raw":
                    continue
                if due[tier](d):
                    # 周按開始那天定月份不準，統一用時段中點
                    when = d["when"] - timedelta(days=3) if d["tier"] == "week" else d["when"] - timedelta(seconds=1)
                    key = _period(tier, when)[0]
                    groups.setdefault((d["scope"], key), []).append(d)
                    reps.setdefault((d["scope"], key), when)
            for (scope, key), low in groups.items():
                same = [d for d in docs if d["tier"] == tier and d["scope"] == scope and d["meta"].get("lively_period") == key]
                plans.append((tier, scope, reps[(scope, key)], same + low))
        # 先壓老的，免得一次跑不完時新的擠掉老的
        plans.sort(key=lambda p: (TIERS.index(p[0]), p[2]))
        return plans

    # ---------- 合併 ----------
    async def _merge(self, eng, tier: str, scope: str, rep: datetime, group: list[dict]) -> tuple[int, int]:
        if len(group) > BATCH:
            group = sorted(group, key=lambda x: x["when"])
            made = archived = 0
            for i in range(0, len(group), BATCH):
                n, a = await self._merge(eng, tier, scope, rep, group[i:i + BATCH])
                made, archived = made + n, archived + a
            return made, archived
        key, start, end, label = _period(tier, rep)
        # 只有一條原始記憶的一天：不用模型，直接標成日記憶
        if tier == "day" and len(group) == 1 and group[0]["tier"] == "raw":
            d = group[0]
            m = dict(d["meta"], lively_tier="day", lively_period=key, lively_period_end=min(end.timestamp(), time.time()) - 1,
                     lively_label=label)
            await eng.db_connection.execute("UPDATE documents SET metadata = ? WHERE id = ?",
                                            (json.dumps(m, ensure_ascii=False), d["id"]))
            await eng.db_connection.commit()
            eng._invalidate_search_cache()
            return 0, 0

        people: dict[str, dict] = {}
        freq: Counter = Counter()
        bot = None
        for d in group:
            for p in d["meta"].get("participant_identities") or []:
                if not isinstance(p, dict) or not p.get("sender_id"):
                    continue
                if p.get("is_bot"):
                    bot = bot or p
                    continue
                people.setdefault(str(p["sender_id"]), p)
                freq[str(p["sender_id"])] += 1
        roster = "、".join(f"{people[q].get('display_name') or q}(QQ {q})" for q, _ in freq.most_common(40))
        items = []
        for d in sorted(group, key=lambda x: x["when"]):
            m = d["meta"]
            items.append({
                "时间": m.get("lively_label") or m.get("source_time_label") or d["when"].strftime("%m-%d %H:%M"),
                "回忆": (m.get("persona_summary") or d["text"])[:900],
                "事实": (m.get("key_facts") or [])[:8],
            })
        where = "群" if "GroupMessage" in scope else "私聊对象"
        n_max = max(1, min(MAX_ENTRIES[tier], round(len(group) / 10) or 1))
        system = self.s.fill(MERGE_RULE).format(where=where, period=label, n=n_max, tier=TIER_NAMES[tier],
                                   chars=MAX_CHARS[tier], people=MAX_PEOPLE)
        prov = self.s.provider()
        if prov is None:
            raise RuntimeError("没有可用的模型")
        resp = await asyncio.wait_for(prov.text_chat(
            prompt=f"人物：{roster or '（没有记录）'}\n\n零碎记忆（{len(items)} 条，按时间）：\n"
                   + json.dumps(items, ensure_ascii=False, indent=0),
            system_prompt=system), timeout=180)
        entries = (_json(getattr(resp, "completion_text", "") or "").get("entries") or [])[:n_max]
        entries = [e for e in entries if isinstance(e, dict) and str(e.get("summary") or "").strip()]
        if not entries:
            raise RuntimeError("模型没给出能用的合并结果")

        first = group[0]["meta"]
        src_imp = [float(d["meta"].get("importance") or 0.5) for d in group]
        new_ids = []
        for e in entries:
            summary = str(e["summary"]).strip()[: MAX_CHARS[tier] + 100]
            facts = [str(x).strip() for x in (e.get("key_facts") or []) if str(x).strip()][:6]
            topics = [str(x).strip() for x in (e.get("topics") or []) if str(x).strip()][:6]
            qqs = [str(q) for q in (e.get("qq") or []) if str(q) in people][:MAX_PEOPLE]
            if not qqs:
                qqs = [q for q, _ in freq.most_common(MAX_PEOPLE)]
            idents = [people[q] for q in qqs] + ([bot] if bot else [])
            try:
                imp = float(e.get("importance"))
            except Exception:
                imp = sum(src_imp) / len(src_imp)
            imp = max(0.3, min(PROTECT - 0.01, max(imp, sum(src_imp) / len(src_imp))))
            canonical = summary + (" | " + "；".join(facts) if facts else "")
            meta = {
                "topics": topics, "key_facts": facts,
                "persona_summary": summary, "canonical_summary": canonical, "summary_schema_version": "v2",
                "participants": [people[q].get("display_name") or q for q in qqs] + ([self.s.fill("我(〈名字〉)")] if bot else []),
                "participant_identities": idents,
                "interaction_type": first.get("interaction_type") or ("group_chat" if "GroupMessage" in scope else "private_chat"),
                "source_session_id": first.get("source_session_id") or scope,
                "time_tags": [label], "source_time_label": label,
                "source_time_start": start.isoformat(), "source_time_end": end.isoformat(),
                "summary_quality": "normal",
                "create_time": min(end.timestamp(), time.time()) - 1,
                "lively_tier": tier, "lively_period": key, "lively_label": label,
                "lively_period_end": min(end.timestamp(), time.time()) - 1,
                "lively_sources": [d["id"] for d in group],
            }
            new_ids.append(await eng.add_memory(
                content=canonical, session_id=scope, persona_id=first.get("persona_id"),
                importance=imp, metadata=meta, preserve_create_time=True))
        old = [d["id"] for d in group]
        archived = await eng.archive_memories(old, audit={"lively_merged_into": new_ids, "lively_merged_tier": tier})
        self._log(tier=tier, scope=scope, period=key, sources=old, new=new_ids)
        logger.info(f"[lively_memtier] {scope} {label}：{len(old)} 条 → {len(new_ids)} 条{TIER_NAMES[tier]}")
        return len(new_ids), archived

    async def run(self) -> str:
        if self._busy.locked():
            return "正在压缩，等一下"
        async with self._busy:
            eng = self._engine()
            docs = await self._active(eng)
            before = len(docs)
            plans = self._plan(docs, datetime.now(TZ))
            calls = made = archived = tagged = failed = 0
            for tier, scope, rep, group in plans:
                needs_llm = not (tier == "day" and len(group) == 1 and group[0]["tier"] == "raw")
                if needs_llm and calls >= MAX_CALLS:
                    break
                try:
                    n, a = await self._merge(eng, tier, scope, rep, group)
                    calls += needs_llm
                    made += n
                    archived += a
                    tagged += not needs_llm
                except Exception as e:
                    calls += needs_llm
                    failed += 1
                    logger.warning(f"[lively_memtier] {scope} {tier} {rep:%Y-%m-%d} 压缩失败: {e}")
            after = len(await self._active(eng))
            msg = (f"记忆分层：{before} 条 → {after} 条（新写 {made} 条分层记忆，归档 {archived} 条，"
                   f"直接标记 {tagged} 条，失败 {failed} 组，调用模型 {calls} 次）")
            if calls >= MAX_CALLS:
                msg += "；今天的额度用完了，剩下的明天接着压"
            self.state["last_result"] = datetime.now(TZ).strftime("%m-%d %H:%M ") + msg
            self._save()
            logger.info(f"[lively_memtier] {msg}")
            return msg

    # ---------- 主人指令 ----------
    async def _status(self) -> str:
        docs = await self._active(self._engine())
        c = Counter(d["tier"] for d in docs)
        prot = sum(1 for d in docs if d["tier"] == "raw" and float(d["meta"].get("importance") or 0) >= PROTECT)
        return (f"记忆分层：{'开' if self.state['enabled'] else '关'}，每天 {RUN_AT} 压缩\n"
                f"现在有效记忆 {len(docs)} 条：零碎 {c['raw']}（其中重要不压的 {prot}），"
                f"日 {c['day']}，周 {c['week']}，月 {c['month']}，季 {c['quarter']}\n"
                f"上次：{self.state['last_result'] or '还没跑过'}")

    async def _run_bg(self, event: AstrMessageEvent):
        try:
            msg = await self.run()
        except Exception as e:
            logger.error(f"[lively_memtier] 手动压缩失败: {e}", exc_info=True)
            msg = f"压缩出错了：{str(e)[:100]}"
        await self.context.send_message(event.unified_msg_origin, MessageChain().message(self.s.owner_text(msg)))

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=1340)
    async def owner(self, event: AstrMessageEvent):
        if not self.s.is_owner(event.get_sender_id()):
            return
        text = (event.message_str or "").strip().translate(T2S)
        if not text.startswith("记忆分层"):
            return
        arg = text[4:].strip()
        if arg in ("", "状态"):
            try:
                reply = await self._status()
            except Exception as e:
                reply = f"读不到记忆：{e}"
        elif arg in ("现在", "压缩"):
            if self._busy.locked():
                reply = "正在压缩，等一下"
            else:
                asyncio.create_task(self._run_bg(event))
                reply = "开始压缩，好了告诉〈主人〉（一般一两分钟）"
        elif arg in ("开", "开启"):
            self.state["enabled"] = True
            self._save()
            reply = "记忆分层开啦"
        elif arg in ("关", "关闭"):
            self.state["enabled"] = False
            self._save()
            reply = "记忆分层先停了，记忆不再压缩"
        else:
            reply = "记忆分层 状态/现在/开/关"
        event.stop_event()
        await event.send(MessageChain().message(self.s.owner_text(reply)))
