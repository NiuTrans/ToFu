"""Read-only installed-artifact certification, usable without a checkout."""
from . import load_kernel


def main():
    kernel = load_kernel()
    if kernel.solve([0, 1], [0], [1.0], [1.0]) != [1.0]:
        raise RuntimeError('native repository kernel failed its installed-artifact probe')
    print('native repository kernel ready')


if __name__ == '__main__':
    main()
