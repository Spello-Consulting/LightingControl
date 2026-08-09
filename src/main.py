"""Main module for the Lighting Control app."""

import argparse
import os
import platform
import signal
import sys
from pathlib import Path
from threading import Event

from dotenv import load_dotenv
from mergedeep import merge
from sc_foundation import (
    RestartPolicy,
    SCCommon,
    SCConfigManager,
    SCLogger,
    ThreadManager,
)
from sc_smart_device import SCSmartDevice, SmartDeviceWorker, smart_devices_validator

from config_schemas import ConfigSchema
from controller import LightingController
from webapp import create_asgi_app, serve_asgi_blocking

CONFIG_FILE = "config.yaml"


def parse_command_line_args() -> dict[str, str | None]:
    """Parse and validate command line arguments.

    Returns:
        dict: Dictionary containing parsed arguments with keys:
            - 'config_file': Path to configuration file (always present)
            - 'homedir': Project home directory (may be None)
    """
    parser = argparse.ArgumentParser(
        description="LightingControl - Intelligent lighting management system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py
  python main.py --config /path/to/config.yaml
  python main.py --homedir /opt/lightingcontrol --config config.yaml
        """
    )

    parser.add_argument(
        "--homedir",
        type=str,
        metavar="PATH",
        help="Specify the project home directory",
    )

    parser.add_argument(
        "--config",
        type=str,
        metavar="FILE",
        help=f"Path to configuration file (default: {CONFIG_FILE})",
    )

    args = parser.parse_args()

    if args.homedir:
        homedir = Path(args.homedir)
        if not homedir.exists():
            print(f"ERROR: Specified homedir does not exist: {args.homedir}", file=sys.stderr)
            sys.exit(1)
        if not homedir.is_dir():
            print(f"ERROR: Specified homedir is not a directory: {args.homedir}", file=sys.stderr)
            sys.exit(1)
        base_dir = homedir.resolve()
        os.environ["SC_FOUNDATION_PROJECT_ROOT"] = str(base_dir)
    else:
        base_dir = Path(SCCommon.get_project_root())

    if args.config:
        config_path = Path(args.config)
        if not config_path.is_absolute():
            config_path = base_dir / config_path
        config_file = str(config_path.resolve())
        if not Path(config_file).exists():
            print(f"ERROR: Configuration file does not exist: {config_file}", file=sys.stderr)
            sys.exit(1)
        if not Path(config_file).is_file():
            print(f"ERROR: Configuration path is not a file: {config_file}", file=sys.stderr)
            sys.exit(1)
    else:
        config_file = CONFIG_FILE

    return {
        "config_file": config_file,
        "homedir": str(base_dir) if args.homedir else None,
    }


def report_fatal_heartbeat(logger) -> None:
    """Report a fatal error to the heartbeat monitor and log it."""
    logger.ping_heartbeat(is_fail=True)


def main():  # noqa: PLR0912, PLR0914, PLR0915
    """Main entry point."""
    load_dotenv()  # Load .env file if present (no-op if absent)
    print(f"Starting LightingControl on {platform.system()}")

    wake_event = Event()   # Wakes the controller loop from sleep (e.g. on webhook)
    stop_event = Event()   # Signals all threads to stop

    cmd_args = parse_command_line_args()

    schemas = ConfigSchema()
    merged_schema = merge({}, smart_devices_validator, schemas.validation)
    assert isinstance(merged_schema, dict)

    try:
        config_file = cmd_args["config_file"]
        assert isinstance(config_file, str)
        config = SCConfigManager(
            config_file=config_file,
            validation_schema=merged_schema,
            placeholders=schemas.placeholders,
        )
    except RuntimeError as e:
        print(f"Configuration file error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        heartbeat_config = config.get("HeartbeatMonitor")
        logger = SCLogger(config.get_logger_settings(), heartbeat_config=heartbeat_config)
    except RuntimeError as e:
        print(f"Logger initialisation error: {e}", file=sys.stderr)
        sys.exit(1)

    logger.log_message("", "summary")
    logger.log_message("", "summary")
    logger.log_message("LightingControl application starting.", "summary")
    if cmd_args["homedir"]:
        logger.log_message(f"Home directory: {cmd_args['homedir']}", "debug")
    logger.log_message(f"Configuration file: {cmd_args['config_file']}", "debug")

    email_settings = config.get_email_settings()
    if email_settings is not None:
        logger.register_email_settings(email_settings)

    if logger.get_fatal_error():
        logger.log_message("Run was successful after a prior failure.", "summary")
        logger.clear_fatal_error()
        logger.send_email("LightingControl recovery", "LightingControl run was successful after a prior failure.")

    # Initialise the smart device layer
    smart_switch_settings = config.get("SCSmartDevices")
    if smart_switch_settings is None:
        logger.log_fatal_error("No SmartDevices settings found in the configuration file.")
        sys.exit(1)
    assert isinstance(smart_switch_settings, dict)

    try:
        smart_switch_control = SCSmartDevice(logger, smart_switch_settings, wake_event)
    except RuntimeError as e:
        logger.log_fatal_error(f"SCSmartDevice initialisation error: {e}")
        sys.exit(1)

    logger.log_message(f"SCSmartDevice initialised with {len(smart_switch_control.devices)} devices.", "summary")

    controller = None
    try:
        smart_device_worker = SmartDeviceWorker(smart_switch_control, logger, wake_event)
        controller = LightingController(config, logger, smart_device_worker, wake_event)

        webapp_enabled = config.get("Website", "Enable", default=False)
        asgi_app = None
        if webapp_enabled:
            asgi_app, web_notifier = create_asgi_app(controller, config, logger)
            controller.set_webapp_notifier(web_notifier.notify)
            _expected_key = os.environ.get("WEBAPP_ACCESS_KEY")  # noqa: RUF052
            if not _expected_key:
                _expected_key = config.get("Website", "AccessKey")  # noqa: RUF052
            if _expected_key and str(_expected_key).strip():
                logger.log_message("Web app access key protection is enabled.", "summary")
            else:
                logger.log_message("Web app is accessible without an access key.", "summary")
    except (RuntimeError, TypeError) as e:
        logger.log_fatal_error(f"Fatal error at startup: {e}")
        sys.exit(1)

    tm = ThreadManager(logger, global_stop=stop_event, before_exit=lambda: report_fatal_heartbeat(logger))

    tm.add(
        name="smart device",
        target=smart_device_worker.run,
        restart=RestartPolicy(mode="on_crash", max_restarts=3, backoff_seconds=2.0),
        stop_event=stop_event,
    )

    tm.add(
        name="controller",
        target=controller.run,
        kwargs={"stop_event": stop_event},
        restart=RestartPolicy(mode="never"),
    )

    if asgi_app is not None:
        tm.add(
            name="webapp",
            target=serve_asgi_blocking,
            args=(asgi_app, config, logger, stop_event),
            restart=RestartPolicy(mode="on_crash", max_restarts=3, backoff_seconds=2.0),
        )

    tm.start_all()

    def _handle_sigterm(signum, frame):
        logger.log_message("SIGTERM received. Shutting down...", "summary")
        stop_event.set()
        wake_event.set()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        while not stop_event.is_set():
            if tm.any_crashed():
                logger.log_fatal_error("A managed thread crashed. Initiating shutdown.", report_stack=False)
                report_fatal_heartbeat(logger)
                stop_event.set()
                wake_event.set()
                break
            stop_event.wait(timeout=1.0)
    except KeyboardInterrupt:
        logger.log_message("KeyboardInterrupt received. Shutting down...", "summary")
        stop_event.set()
        wake_event.set()
    finally:
        tm.stop_all()
        tm.join_all(timeout_per_thread=10.0)
        logger.log_message("LightingControl application stopped.", "summary")


if __name__ == "__main__":
    main()
