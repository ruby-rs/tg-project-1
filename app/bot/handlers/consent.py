"""Согласие на обработку персональных данных (152-ФЗ).

Пока согласия нет, бот не принимает ни сообщений, ни команд: роутер стоит первым
и перехватывает всё. Параметр /start (ссылка-приглашение) запоминается и
применяется сразу после согласия.
"""

from datetime import UTC, datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, Filter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.start import process_start
from app.config import Settings
from app.db.models import User

router = Router(name="consent")

START_ARGS_KEY = "consent_start_args"


class ConsentAction(CallbackData, prefix="consent"):
    action: str  # accept | revoke


class NeedsConsent(Filter):
    async def __call__(self, event: TelegramObject, user: User | None = None) -> bool:
        return user is not None and user.consent_at is None


def consent_text(settings: Settings) -> str:
    lines = [
        "🔒 <b>Согласие на обработку персональных данных</b>",
        "",
        "Чтобы пользоваться ботом, нужно ваше согласие на обработку персональных данных "
        "(152-ФЗ): имени и username в Telegram, а также фото, голосовых, видео и текстов, "
        "которые вы присылаете. Голосовые расшифровываются автоматически, по сообщениям "
        "составляются отчёты для руководителя вашей компании.",
        "",
        "Цель — ведение отчётности по строительным объектам компании, в которой вы работаете. "
        "Данные хранятся, пока вы пользуетесь ботом, и могут храниться дольше, если это "
        "нужно компании как доказательство выполненных работ.",
    ]
    if settings.pd_operator:
        lines += ["", f"Оператор: {escape(settings.pd_operator)}"]
    if settings.privacy_policy_url:
        lines += [
            "",
            f'<a href="{escape(settings.privacy_policy_url)}">Политика конфиденциальности</a>',
        ]
    lines += ["", "Отозвать согласие можно в любой момент командой /privacy."]
    return "\n".join(lines)


def consent_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Согласен", callback_data=ConsentAction(action="accept"))
    return kb.as_markup()


@router.message(CommandStart(), NeedsConsent())
async def start_without_consent(
    message: Message, command: CommandObject, state: FSMContext, settings: Settings
) -> None:
    await state.clear()
    await state.update_data({START_ARGS_KEY: command.args or ""})
    await message.answer(consent_text(settings), reply_markup=consent_keyboard())


@router.message(NeedsConsent())
async def message_without_consent(message: Message, settings: Settings) -> None:
    await message.answer(
        consent_text(settings) + "\n\nПосле согласия пришлите сообщение ещё раз.",
        reply_markup=consent_keyboard(),
    )


@router.callback_query(ConsentAction.filter(F.action == "accept"))
async def on_accept(
    call: CallbackQuery,
    bot: Bot,
    user: User,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if user.consent_at is None:
        user.consent_at = datetime.now(UTC)
    args = (await state.get_data()).get(START_ARGS_KEY, "")
    await call.answer("Спасибо!")
    chat_id = call.message.chat.id if call.message else call.from_user.id
    if call.message:
        await call.message.edit_reply_markup(reply_markup=None)
    await process_start(bot, chat_id, args, user, session, state)


@router.callback_query(NeedsConsent())
async def callback_without_consent(call: CallbackQuery) -> None:
    await call.answer("Сначала дайте согласие на обработку данных: /start", show_alert=True)


@router.message(Command("privacy"))
async def cmd_privacy(message: Message, settings: Settings) -> None:
    lines = ["🔒 <b>Персональные данные</b>", ""]
    if settings.pd_operator:
        lines.append(f"Оператор: {escape(settings.pd_operator)}")
    if settings.privacy_policy_url:
        lines.append(
            f'<a href="{escape(settings.privacy_policy_url)}">Политика конфиденциальности</a>'
        )
    lines.append(
        "\nЕсли отозвать согласие, бот перестанет принимать ваши сообщения. "
        "Уже отправленные сообщения остаются в отчётах компании."
    )
    if settings.support_contact:
        lines.append(f"Чтобы удалить свои данные, напишите: {escape(settings.support_contact)}")
    kb = InlineKeyboardBuilder()
    kb.button(text="Отозвать согласие", callback_data=ConsentAction(action="revoke"))
    await message.answer("\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(ConsentAction.filter(F.action == "revoke"))
async def on_revoke(call: CallbackQuery, user: User, state: FSMContext) -> None:
    user.consent_at = None
    await state.clear()
    await call.answer()
    if call.message:
        await call.message.edit_text(
            "Согласие отозвано. Чтобы снова пользоваться ботом, нажмите /start."
        )
