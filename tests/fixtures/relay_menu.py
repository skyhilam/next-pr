"""Inert, two-minute TUI fixture. Only renders labels and reads constrained keys."""
import os
import select
import sys
import termios
import time
import tty


def main():
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    selected, count = 0, 0
    labels = ('Blue', 'Green')
    deadline = time.monotonic() + 120
    result = 'Fixture timed out; nothing was authorized or executed.'
    try:
        tty.setcbreak(fd)
        sys.stdout.write('\x1b[?1049h')
        while time.monotonic() < deadline:
            print('\x1b[2J\x1b[HINERT MENU FIXTURE — labels only', flush=True)
            print('Arrow keys or 1/2; Enter to confirm. No task or PR actions.')
            for index, label in enumerate(labels):
                print(('> ' if index == selected else '  ') + f'{index + 1}. {label}')
            print(f'Selected: {labels[selected]} | Keys received: {count}', flush=True)
            if not select.select([fd], [], [], max(0, deadline - time.monotonic()))[0]:
                break
            key = os.read(fd, 1)
            if key == b'\x1b':
                for _ in range(2):
                    if select.select([fd], [], [], .25)[0]:
                        key += os.read(fd, 1)
            if key not in (b'\x1b[A', b'\x1b[B', b'1', b'2', b'\r', b'\n'):
                continue
            count += 1
            if key in (b'\r', b'\n'):
                result = f'FIXTURE CONFIRMED: {labels[selected]} | Keys received: {count}'
                break
            selected = (max(0, selected - 1) if key == b'\x1b[A' else
                        min(1, selected + 1) if key == b'\x1b[B' else int(key) - 1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)
        print('\x1b[?1049l' + result, flush=True)


if __name__ == '__main__':
    main()
