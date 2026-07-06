from aiogram import Dispatcher

from app.handlers.moderation import router as moderation_router
from app.handlers.settings import router as settings_router
from app.handlers.sources import router as sources_router
from app.handlers.start import router as start_router
from app.handlers.viewer import router as viewer_router
from app.handlers.workspace import router as workspace_router


def setup_routers(dp: Dispatcher) -> None:
    dp.include_router(workspace_router)
    dp.include_router(moderation_router)
    dp.include_router(sources_router)
    dp.include_router(settings_router)
    dp.include_router(viewer_router)
    dp.include_router(start_router)
