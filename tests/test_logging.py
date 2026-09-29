import logging

from goldbot.monitoring.logging_setup import SecretRedactor, setup_logging


def test_redactor_masks_secrets_in_formatted_messages():
    redactor = SecretRedactor(["87654321", "hunter2", ""])
    record = logging.LogRecord(
        "goldbot", logging.INFO, __file__, 1, "login %s password %s", ("87654321", "hunter2"), None
    )
    redactor.filter(record)
    assert record.getMessage() == "login *** password ***"


def test_log_file_is_utc_and_never_contains_secrets(tmp_path):
    logger = setup_logging(
        log_dir=tmp_path,
        level="INFO",
        max_bytes=10_000,
        backup_count=2,
        display_timezone="Europe/Paris",
        secrets=["87654321", "Axi-Demo-Server"],
    )
    logging.getLogger("goldbot.test").warning("connexion au serveur %s refusée", "Axi-Demo-Server")
    for handler in logger.handlers:
        handler.flush()
    content = (tmp_path / "goldbot.log").read_text(encoding="utf-8")
    assert "Axi-Demo-Server" not in content
    assert "connexion au serveur *** refusée" in content
    assert content.split(" ", 1)[0].endswith("Z")  # horodatage UTC


def test_a_message_already_printed_goes_to_the_file_only(tmp_path, capsys):
    logger = setup_logging(
        log_dir=tmp_path, level="INFO", max_bytes=10_000, backup_count=2, display_timezone="Europe/Paris"
    )
    logging.getLogger("goldbot.test").info("déjà affiché", extra={"console": False})
    logging.getLogger("goldbot.test").warning("avertissement")
    for handler in logger.handlers:
        handler.flush()
    console = capsys.readouterr().err
    assert "déjà affiché" not in console and "avertissement" in console
    content = (tmp_path / "goldbot.log").read_text(encoding="utf-8")
    assert "déjà affiché" in content and "avertissement" in content


def test_setup_logging_is_idempotent(tmp_path):
    kwargs = dict(log_dir=tmp_path, level="INFO", max_bytes=10_000, backup_count=2, display_timezone="Europe/Paris")
    setup_logging(**kwargs)
    logger = setup_logging(**kwargs)
    assert len(logger.handlers) == 2
