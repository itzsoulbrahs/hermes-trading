"""Entry point for Hermes Trading Worker."""

from .loop import main as run_loop_main


def main():
    run_loop_main()


if __name__ == "__main__":
    main()