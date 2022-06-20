import asyncio

import kopf

def main():
    loop = asyncio.get_event_loop()
    loop.run_until_complete(kopf.operator())

if __name__ == "__main__":
    main()