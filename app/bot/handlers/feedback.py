"""Оценка отчётов (👍/👎 под отчётом) и метрики пилота (/stats)."""

from datetime import date, timedelta
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import HasCompany, IsManager
from app.bot.handlers.reports import enqueue_report
from app.bot.keyboards import Feedback, cancel_keyboard
from app.bot.states import FeedbackComment
from app.config import Settings
from app.db.models import EntryKind, User
from app.db.repositories import CompanyStats, EntryRepo, FeedbackRepo, SiteRepo, StatsRepo
from app.reports.render import TG_MESSAGE_LIMIT
from app.timeutils import today_for

router = Router(name="feedback")
router.message.filter(HasCompany())
router.callback_query.filter(HasCompany())

STATS_DAYS = 7

_KIND_ICONS = {
    EntryKind.TEXT: "✏️",
    EntryKind.VOICE: "🎙",
    EntryKind.AUDIO: "🎵",
    EntryKind.VIDEO_NOTE: "📹",
    EntryKind.PHOTO: "📷",
    EntryKind.VIDEO: "🎬",
    EntryKind.DOCUMENT: "📎",
}


class RetryFailed(CallbackData, prefix="retry"):
    pass


# ---------- Оценка отчёта ----------


@router.callback_query(Feedback.filter())
async def on_feedback(
    call: CallbackQuery,
    callback_data: Feedback,
    user: User,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    site = await SiteRepo(session).get_for_user(user, callback_data.site_id)
    if site is None or callback_data.rating not in (1, -1):
        await call.answer("Отчёт не найден", show_alert=True)
        return
    work_date = date.fromordinal(callback_data.day)
    await FeedbackRepo(session).rate(site.id, work_date, user.id, callback_data.rating)
    if callback_data.rating > 0:
        await call.answer("Спасибо!")
        return
    await call.answer()
    await state.set_state(FeedbackComment.text)
    await state.update_data(site_id=site.id, day=callback_data.day)
    if call.message:
        await call.message.answer(
            "Что не так в отчёте? Напишите коротко: неверный объём, пропущена работа, "
            "лишнее и т.п. Я пересоберу отчёт с учётом замечания.",
            reply_markup=cancel_keyboard("Пропустить"),
        )


@router.message(FeedbackComment.text, F.text & ~F.text.startswith("/"))
async def on_feedback_comment(
    message: Message, bot: Bot, user: User, session: AsyncSession, state: FSMContext
) -> None:
    data = await state.get_data()
    await state.clear()
    site = None
    if "site_id" in data and "day" in data:
        work_date = date.fromordinal(data["day"])
        await FeedbackRepo(session).comment(
            data["site_id"], work_date, user.id, message.text.strip()
        )
        site = await SiteRepo(session).get_for_user(user, data["site_id"])
    await message.answer("Спасибо, замечание записал и учту его в отчёте.")
    if site is not None:
        await enqueue_report(bot, message.chat.id, site, work_date, user, session, rebuild=True)


# ---------- Статистика для руководителя ----------


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    minutes, sec = divmod(int(seconds), 60)
    return f"{minutes} мин {sec} с" if minutes else f"{sec} с"


def _pct(part: int, total: int) -> str:
    return f"{round(100 * part / total)}%" if total else "—"


def render_stats(st: CompanyStats) -> str:
    total = sum(st.kinds.values())
    by_kind = " ".join(
        f"{_KIND_ICONS.get(k, '•')}{n}" for k, n in sorted(st.kinds.items(), key=lambda kv: -kv[1])
    )
    lines = [
        f"📊 <b>Статистика за {st.date_from:%d.%m}–{st.date_to:%d.%m}</b>",
        "",
        f"Сообщений: {total} {by_kind}".rstrip(),
        f"🎙 Расшифровано голосовых: {st.transcribed} из {st.voices}",
        f"✍️ Исправлено прорабами: {st.corrected} ({_pct(st.corrected, st.transcribed)})",
        "",
        f"📋 Отчётов собрано: {st.reports_done}, с ошибкой: {st.reports_failed}",
        f"⏱ Среднее время сборки: {_fmt_duration(st.report_avg_seconds)}",
        f"Оценки отчётов: 👍 {st.likes} · 👎 {st.dislikes}",
    ]
    if st.comments:
        lines += ["", "<b>Последние замечания к отчётам:</b>"]
        lines += [f"• «{escape(c[:200])}»" for c in st.comments]
    if st.activity:
        lines += ["", f"<b>Активность прорабов</b> (дней с сообщениями из {STATS_DAYS}):"]
        lines += [f"• {escape(name)} — {days}" for name, days in st.activity]
    if st.unprocessed or st.failed:
        lines += ["", f"⏳ В обработке: {st.unprocessed} · ❌ Не обработано: {st.failed}"]
    return "\n".join(lines)[:TG_MESSAGE_LIMIT]


def stats_keyboard(st: CompanyStats) -> InlineKeyboardMarkup | None:
    if not st.failed:
        return None
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🔁 Повторить обработку ({st.failed})", callback_data=RetryFailed())
    return kb.as_markup()


@router.message(Command("stats"), IsManager())
async def cmd_stats(
    message: Message, user: User, session: AsyncSession, settings: Settings
) -> None:
    today = today_for(user.company.timezone, settings.work_day_start_hour)
    stats = await StatsRepo(session).collect(
        user.company_id, today - timedelta(days=STATS_DAYS - 1), today
    )
    await message.answer(render_stats(stats), reply_markup=stats_keyboard(stats))


@router.callback_query(RetryFailed.filter(), IsManager())
async def on_retry_failed(call: CallbackQuery, user: User, session: AsyncSession) -> None:
    count = await EntryRepo(session).retry_failed(user.company_id)
    await call.answer(f"Вернул в обработку: {count}", show_alert=True)
