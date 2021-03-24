import asyncio
import concurrent.futures
import logging
import os
import time
import typing as tp

from aiogram import Bot, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import Dispatcher
from aiogram.types import ReplyKeyboardRemove
from aiogram.utils import executor
from aiogram.utils.emoji import emojize
from aiogram.utils.helper import Helper, HelperMode, ListItem

from config import *

# Bot initialization
TOKEN = os.environ.get('TOKEN', None)
bot = Bot(token=TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())

# Logging
logging.basicConfig(filename='log.txt',
                    filemode='a',
                    format='%(asctime)s, %(msecs) d %(name)s %(levelname) s %(message) s',
                    datefmt='%H:%M:%S',
                    level=logging.INFO)
logging.info("Model was init")

# Limit parallel processes
sem = asyncio.Semaphore(2)

# Global video storage
user_videos = dict()


class TestStates(Helper):
    mode = HelperMode.snake_case

    TEST_STATE_0 = ListItem()
    TEST_STATE_1 = ListItem()


def first_order(user_id: int):
    time.sleep(60)
    return user_id


def safe_first_order(user_id: int):
    """
    Safe run of first_order - Semaphore limits max processes in parallel
    """
    start = time.time()
    res = first_order(user_id)
    end = time.time()
    logging.info(f"Video processing took {end - start}")
    return res


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

    logging.info(f"Took video. Duration: {video.duration} sec")
    if video.duration > 60:
        await message.answer("Видео слишком длинное. "
                             "Поддерживаются видео продолжительностью не более 1 минуты")
        logging.info("Took too long video")
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
        async with sem:
            result = await loop.run_in_executor(
                pool, safe_first_order, message.from_user.id)

    await message.answer(f"Отправляю обработанное видео {result}")


@dp.message_handler(commands=['start'])
async def send_welcome(message: types.Message) -> None:
    await message.answer("Привет, {}!\n".format(message.from_user.first_name) +
                         "Я бот, который поможет тебе предстать в совершенно новом облике\n"
                         "Отправь видео, в котором ты хочешь оказаться")


@dp.message_handler(commands=['help'])
async def send_help(message: types.Message) -> None:
    logging.info(f"User {message.from_user.id} asked for help")
    await message.answer("Нужна помощь? Решение очень простое!\n" +
                         "Просто отправь видео, в котором ты хочешь появиться, и бот сделает все за тебя.\n" +
                         "Если ты еще не отправлял свое фото/видео, или хочешь его поменять, "
                         "бот предоставит тебе такую возможность после выбора таргетного видео.")


@dp.message_handler(content_types=['video', 'video_note'])
async def handle_target_video(message: types.Message):
    if not await save_video(message, 'target'):
        return

    if user_videos[message.from_user.id]['source'] is None:
        await ask_for_source(message)
    else:
        await change_state(message.from_user.id, 0)
        await message.answer("Я могу начать обработку видео. "
                             "Поменять фото/видео которое будем переносить на таргет, или продолжим?",
                             reply_markup=markup_source)


@dp.message_handler()
async def handle_text(message: types.Message):
    await message.answer("Жду видео:)")


@dp.message_handler(state=TestStates.TEST_STATE_0)
async def choose_source_video(message: types.Message):
    if message.text.strip() == CHANGE_VIDEO:
        await ask_for_source(message)
    elif message.text.strip() == CONTINUE:
        await process_video(message)
    else:
        await message.answer("Немного не понял, выбери пожалуйста вариант внизу",
                             reply_markup=markup_source)


@dp.message_handler(state=TestStates.TEST_STATE_1, content_types=['video', 'video_note'])
async def handle_source_video(message: types.Message):
    if not await save_video(message, 'source'):
        return

    await process_video(message)


if __name__ == '__main__':
    executor.start_polling(dp)
