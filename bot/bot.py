import logging
import os

from aiogram import Bot
from aiogram.utils import executor
from aiogram.dispatcher import Dispatcher
from aiogram.contrib.fsm_storage.memory import MemoryStorage

from config import *
from utils import *

TOKEN = os.environ.get('TOKEN', None)
bot = Bot(token=TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())

logging.basicConfig(filename='log.txt',
                    filemode='a',
                    format='%(asctime)s, %(msecs) d %(name)s %(levelname) s %(message) s',
                    datefmt='%H:%M:%S',
                    level=logging.INFO)
logging.info("Model was init")

sem = asyncio.Semaphore(5)
user_videos = dict()


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
