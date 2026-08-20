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
                "DEBUG": "cyan",
                "INFO": "green",
                "WARNING": "fg_202",
                "ERROR": "bg_1",
                "CRITICAL": "bold_red",
            },
        )
    )

    logging.basicConfig(level=logging.DEBUG, handlers=[handler])
    return logger
