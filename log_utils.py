import logging
import colorlog


def get_logger():
    logger = logging.getLogger("PROMATIC-PROXY-SERVER")

    handler = colorlog.StreamHandler()
    handler.setFormatter(
        colorlog.ColoredFormatter(
            "%(log_color)s%(asctime)s %(levelname)-8s%(reset)s %(message)s",
            datefmt="%H:%M:%S",
            log_colors={
                "DEBUG": "fg_236",
                "INFO": "green",
                "WARNING": "fg_214",
                "ERROR": "bg_214",
                "CRITICAL": "bg_1",
            },
        )
    )

    logging.basicConfig(level=logging.DEBUG, handlers=[handler])
    return logger
