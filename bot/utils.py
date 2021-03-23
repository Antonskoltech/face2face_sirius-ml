import asyncio
import concurrent.futures
import time
import typing as tp

from aiogram import types
from aiogram.utils.emoji import emojize
from aiogram.types import ReplyKeyboardRemove
from aiogram.utils.helper import Helper, HelperMode, ListItem

from config import EMPTY_DICT
from bot import bot, dp, sem, user_videos


class TestStates(Helper):
    mode = HelperMode.snake_case

    TEST_STATE_0 = ListItem()
    TEST_STATE_1 = ListItem()


def first_order(user_id: int):
    time.sleep(30)
    return user_id


async def safe_first_order(user_id: int):
    """
    Safe run of first_order - Semaphore limits max processes in parallel
    """
    async with sem:
        return first_order(user_id)


async def change_state(user_id: int, new_state: tp.Optional[int]):
    state = dp.current_state(user=user_id)
    if new_state is None or new_state >= len(TestStates.all()):
        await state.reset_state()
    else:
        await state.set_state(TestStates.all()[new_state])


async def ask_for_source(message: types.Message):
    await change_state(message.from_user.id, 1)
    await message.answer("А теперь загрузи свое фото/видео, "
                         "чтобы мы смогли его представить в новом облике",
                         reply_markup=ReplyKeyboardRemove())


async def save_video(message: types.Message, key: str):
    if message.content_type == 'video':
        video = message.video
    else:
        video = message.video_note

    if video.duration > 60:
        await message.answer("Видео слишком длинное. "
                             "Поддерживаются видео продолжительностью не более 1 минуты")
        return False

    meta = await video.get_file()
    resp = await bot.download_file(meta['file_path'])
    target = resp.read()

    await video.download("1.mp4")
    if message.from_user.id not in user_videos:
        user_videos[message.from_user.id] = EMPTY_DICT

    user_videos[message.from_user.id][key] = target
    await message.answer("Видео успешно загружено")
    return True


async def process_video(message: types.Message):
    await message.answer("Начал обработку видео",
                         reply_markup=ReplyKeyboardRemove())
    await message.answer(emojize(":hourglass_flowing_sand:"))
    await change_state(message.from_user.id, None)

    # Run first order model
    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        result = await loop.run_in_executor(
            pool, safe_first_order, message.from_user.id)

    await message.answer(f"Отправляю обработанное видео {result}")
