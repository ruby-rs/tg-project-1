"""Экраны меню: одно сообщение, которое меняется на месте.

Нажатие кнопки правит то же сообщение, а не присылает новое. Где нужен ввод текста
(название объекта, время), вопрос задаётся в том же сообщении; ответ пользователя
удаляется, а сообщение с вопросом заменяется результатом. Новыми сообщениями
приходят только результаты: отчёт, медиа к нему и архив.
"""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, ReplyKeyboardRemove

log = logging.getLogger(__name__)

PROMPT_KEY = "prompt_message_id"

Event = Message | CallbackQuery


async def show(
    event: Event, text: str, markup: InlineKeyboardMarkup | None = None
) -> Message | None:
    """Команда — новое сообщение; нажатие кнопки — правка сообщения с этой кнопкой."""
    if isinstance(event, Message):
        return await event.answer(text, reply_markup=markup)
    try:
        await event.answer()
    except TelegramBadRequest:
        pass  # на нажатие уже ответили (например, всплывающим уведомлением)
    message = event.message
    if not isinstance(message, Message):
        return None
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return message
        # Сообщение слишком старое или это медиа — тогда отправляем новое
        return await message.answer(text, reply_markup=markup)
    return message


async def ask(
    event: Event, state: FSMContext, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    """Вопрос, ответ на который пользователь напишет текстом. Состояние ставит вызывающий."""
    message = await show(event, text, markup)
    if message is not None:
        await state.update_data({PROMPT_KEY: message.message_id})


async def reply_to_input(
    message: Message, data: dict, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    """Ответ на введённый текст: удаляем сообщение пользователя, правим вопрос."""
    bot = message.bot
    prompt_id = data.get(PROMPT_KEY)
    await delete_quietly(bot, message.chat.id, message.message_id)
    if prompt_id:
        try:
            await bot.edit_message_text(
                text, chat_id=message.chat.id, message_id=prompt_id, reply_markup=markup
            )
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                return
    await message.answer(text, reply_markup=markup)


async def delete_quietly(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        log.debug("Не удалось удалить сообщение %s в чате %s", message_id, chat_id)


async def drop_reply_keyboard(bot: Bot, chat_id: int) -> None:
    """Убирает клавиатуру под полем ввода, оставшуюся от прежней версии меню.

    Убрать её можно только сообщением, поэтому отправляем служебное и сразу удаляем.
    """
    try:
        sent = await bot.send_message(chat_id, "…", reply_markup=ReplyKeyboardRemove())
    except TelegramAPIError:
        return
    await delete_quietly(bot, chat_id, sent.message_id)
