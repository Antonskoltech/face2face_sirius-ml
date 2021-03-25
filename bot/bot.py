import asyncio
import concurrent.futures
import logging
import os
import sys
import time
import typing as tp

from aiogram import Bot, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import Dispatcher
from aiogram.types import ReplyKeyboardRemove
from aiogram.utils import executor, exceptions
from aiogram.utils.emoji import emojize
from aiogram.utils.helper import Helper, HelperMode, ListItem
import imageio
from moviepy.editor import *
from skimage import img_as_ubyte
from skimage.transform import resize

from config import *

sys.path.append("../first-order-model")
from demo import make_animation, make_photo_animation, read_video, load_checkpoints, super_resolution
from crop import crop_image, crop_video

# Bot initialization
TOKEN = os.environ.get('TOKEN', None)
bot = Bot(token=TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())

# Logging
# logging.basicConfig(filename='log.txt',
#                     filemode='a',
#                     format='%(asctime)s, %(msecs) d %(name)s %(levelname) s %(message) s',
#                     datefmt='%H:%M:%S',
#                     level=logging.INFO)

logging.info("Model was init")

# Limit parallel processes
sem = asyncio.Semaphore(5)

# Global video storage
user_videos = dict()


class TestStates(Helper):
    mode = HelperMode.snake_case

    TEST_STATE_0 = ListItem()
    TEST_STATE_1 = ListItem()


RELATIVE = True
ADAPT_SCALE = True
CPU = False
CONFIG = '../first-order-model/config/vox-256.yaml'
CHECKPOINT = '../first-order-model/pretrained_models/vox-cpk.pth.tar'
PATH = 'img/'


def prepare_data(user_id: int):
    data = dict()
    source = user_videos[user_id]['source']
    target = user_videos[user_id]['target']

    audio_clip = AudioFileClip(target)
    data['audio'] = audio_clip

    if source.endswith('.jpg'):
        crop_image(source)
    else:
        # pass
        crop_video(source)
    try:
        source_reader = imageio.get_reader('crop_' + source)
    except FileNotFoundError:
        print("Didn't find cropped video")
        source_reader = imageio.get_reader(source)

    if source.endswith('.jpg'):
        data['source_media'] = resize(next(iter(source_reader)), (256, 256))[..., :3]
        data['photo'] = True
    else:
        data['source_media'] = read_video(source_reader)
        data['photo'] = False

    crop_video(target)
    try:
        target_reader = imageio.get_reader('crop_' + target)
    except FileNotFoundError:
        print("Didn't find cropped video")
        target_reader = imageio.get_reader(target)
    fps = target_reader.get_meta_data()['fps']

    data['fps'] = fps
    data['target_media'] = read_video(target_reader)

    generator, kp_detector = load_checkpoints(config_path=CONFIG,
                                              checkpoint_path=CHECKPOINT,
                                              cpu=CPU)
    data['generator'] = generator
    data['kp_detector'] = kp_detector
    return data


def first_order(user_id: int):
    data = prepare_data(user_id)
    if data['photo']:
        predictions = make_photo_animation(
                data['source_media'], data['target_media'],
                data['generator'], data['kp_detector'],
                relative=RELATIVE,
                adapt_movement_scale=ADAPT_SCALE,
                cpu=CPU
        )
    else:
        predictions = make_animation(
                data['source_media'], data['target_media'],
                data['generator'], data['kp_detector'],
                relative=RELATIVE,
                adapt_movement_scale=ADAPT_SCALE,
                cpu=CPU
        )
    # imageio.mimsave(f'{PATH}1.mp4', [img_as_ubyte(frame) for frame in predictions], "mp4", fps=data['fps'])
    filename = f'{PATH}{user_id}'
    imageio.mimsave(filename + '.mp4',
                    [super_resolution(img_as_ubyte(frame), 4) for frame in predictions],
                    "mp4", fps=data['fps'])
    video_clip = VideoFileClip(filename + '.mp4')
    video_clip.audio = data['audio']
    try:
        video_clip.write_videofile(filename + '_a' + '.mp4')
    except Exception as e:
        print(e)
        video_clip = VideoFileClip(filename + '.mp4')
        video_clip.write_videofile(filename + '_a' + '.mp4')


def safe_first_order(user_id: int):
    """
    Safe run of first_order - Semaphore limits max processes in parallel
    """
    start = time.time()
    try:
        first_order(user_id)
    except Exception as e:
        print(e)
        logging.warning(e)
        return False
    end = time.time()
    logging.info(f"Video processing took {end - start}")
    return True


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


async def save_media(message: types.Message, key: str):
    photo = False
    ext = '.mp4'
    if message.content_type == 'video':
        media = message.video
    elif message.content_type == 'video_note':
        media = message.video_note
    elif message.content_type == 'animation':
        media = message.animation
    else:
        media = message.photo[-1]
        photo = True
        ext = '.jpg'

    if photo:
        logging.info(f"Took photo")
    else:
        logging.info(f"Took video. Duration: {media.duration} sec")
        if media.duration > 60:
            await message.answer("Видео слишком длинное. "
                                 "Поддерживаются видео продолжительностью не более 1 минуты")
            logging.info("Took too long video")
            return False

    try:
        filename = f"{PATH}{key}{message.from_user.id}{ext}"
        await media.download(filename)
    except exceptions.FileIsTooBig:
        await message.answer("Телеграм не поддерживает файлы свыше 20 Мб, "
                             "попробуйте отправить ваше видео со сжатием")
        return False

    if message.from_user.id not in user_videos:
        user_videos[message.from_user.id] = EMPTY_DICT

    user_videos[message.from_user.id][key] = filename
    if photo:
        await message.answer("Фото успешно загружено")
    else:
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
            res = await loop.run_in_executor(
                pool, safe_first_order, message.from_user.id)
    if not res:
        await message.answer("В процессе обработки возникла ошибка.\n"
                             "Скорее всего, на одном из видео/фото алгоритм не смог распознать лица.\n"
                             "Попробуй начать сначала и отправить видео, из которого нужно перенести мимику")
    else:
        # await message.answer_video(open(f'{PATH}{message.from_user.id}.mp4', 'rb'))
        await message.answer_video(open(f'{PATH}{message.from_user.id}_a.mp4', 'rb'))
    os.system(f"rm img/target{message.from_user.id}* crop_img/*{message.from_user.id}*")
    # await message.answer(f"Отправляю обработанное видео")


@dp.message_handler(commands=['start'])
async def send_welcome(message: types.Message) -> None:
    user_videos[message.from_user.id] = EMPTY_DICT
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


@dp.message_handler(content_types=['video', 'video_note', 'animation'])
async def handle_target_video(message: types.Message):
    if not await save_media(message, 'target'):
        return

    # if user_videos[message.from_user.id]['source'] is None:
    await ask_for_source(message)
    # else:
    #     await change_state(message.from_user.id, 0)
    #     await message.answer("Я могу начать обработку видео. "
    #                          "Поменять фото/видео человека на которого будем переносить мимику из таргета, "
    #                          "или продолжим?",
    #                          reply_markup=markup_source)


@dp.message_handler()
async def handle_text(message: types.Message):
    await message.answer("Жду видео, из которого нужно перенести мимику:)")


@dp.message_handler(state=TestStates.TEST_STATE_0)
async def choose_source_video(message: types.Message):
    if message.text.strip() == CHANGE_VIDEO:
        await ask_for_source(message)
    elif message.text.strip() == CONTINUE:
        await process_video(message)
    else:
        await message.answer("Немного не понял, выбери пожалуйста вариант внизу",
                             reply_markup=markup_source)


@dp.message_handler(state=TestStates.TEST_STATE_1, content_types=['photo', 'video', 'video_note'])
async def handle_source_video(message: types.Message):
    if not await save_media(message, 'source'):
        return

    await process_video(message)


if __name__ == '__main__':
    executor.start_polling(dp)
