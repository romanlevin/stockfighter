#!/usr/bin/env python
import time
import asyncio
import json
import websockets
import logging
import re

from wrappers import API, Web, APIError

WEBSOCKET = 'wss://api.stockfighter.io/ob/api/ws/{account}/venues/{venue}/tickertape/stocks/{stock}'
THRESHOLD = 0.95

client_logger = logging.getLogger('aiohttp.client')
client_logger.addHandler(logging.StreamHandler())
client_logger.setLevel(logging.INFO)


target_pattern = re.compile('target price is \$(\d+\.\d{2})')


def extract_target(info):
    try:
        return int(float(target_pattern.search(info).groups()[0]) * 100)
    except Exception:
        client_logger.exception("Error extracting client's target")


class State:
    def __init__(self, instance_id, shares_to_buy=100000):
        self.instance_id = instance_id
        self.last = time.time()
        self.shares_to_buy = shares_to_buy
        self.target = None
        self.shares_bought = 0
        self.clients_target = None
        self.trading_day = None
        self.end_of_the_world_day = None

    @property
    def need_more_shares(self):
        return self.shares_bought < self.shares_to_buy

    def initialize_target(self, ask):
        if self.target is None:
            self.target = ask * THRESHOLD

    def evaluate_ask(self, ask):
        if ask > self.target:
            now = time.time()
            if now - self.last > 10:
                # Don't set a target higher than client's target
                self.target = min(self.target * 1.01, self.clients_target or self.target * 1.01)
                self.last = now
            return
        return True

    @property
    def remaining_share_to_buy(self):
        return self.shares_to_buy - self.shares_bought

    def bid_size(self, ask_size):
        return min(self.remaining_share_to_buy, ask_size)

    def fill_order(self, filled):
        self.shares_bought += filled

    def lower_target(self):
        self.target *= THRESHOLD

    def update_last(self):
        self.last = time.time()

    def update_from_instance(self, instance):
        details = instance.get('details')
        if details:
            self.end_of_the_world_day = details.get('endOfTheWorldDay')
            self.trading_day = details.get('tradingDay')

        flash = instance.get('flash')
        try:
            info = flash.get('info')
        except AttributeError:
            info = None

        if info and not self.clients_target:
            target = extract_target(info)
            self.clients_target = target
            client_logger.info('Setting client\'s target: %s', target)

        self.state = instance.get('state')


async def buy_and_update(bid_size, ask, state, api, web):
    order = await api.time_bounded_order(direction='buy', shares=bid_size, order_type='limit', price=ask)
    filled = order['totalFilled']
    state.fill_order(filled)
    if filled:
        client_logger.info('Order %d filled for %d shares', order['id'], filled)
        try:
            instance = await web.instance(state.instance_id)
            flash = instance['flash']
            if 'error' in flash:
                client_logger.error('Screwed the pooch: %s', flash['error'])
                exit(1)
            client_logger.info(instance)
            state.update_from_instance(instance)

        except KeyError:
            client_logger.warning("Couldn't extract flash: %r", instance)
        # in case price will continue going down, lower target
        state.lower_target()
    state.update_last()

async def initialize_instance(web):
    with web:
        api_key = await web.login()
        levels = await web.level()
        instance_id = levels['levels']['chock_a_block']['instanceId']
        try:
            # Clean up previous run if unsucessful
            await web.stop(instance_id)
        except Exception:
            pass
        instance = await web.resume(instance_id)
    return api_key, instance_id, instance

async def keep_buying(to_buy=100000):
    """
    Perform a block buy of a stock.

    Quote is updated in a different task by a websocket.
    """
    loop = asyncio.get_event_loop()
    web = Web()
    api_key, instance_id, instance = await initialize_instance(web)
    account = instance['account']
    stock = instance['tickers'][0]
    venue = instance['venues'][0]
    # buy when ask is below this
    ask = 0
    quote = {}
    state = State(instance_id, shares_to_buy=to_buy)
    ws_url = WEBSOCKET.format(account=account, venue=venue, stock=stock)
    with API(api_key=api_key, venue=venue, account=account, stock=stock) as api, web as web:
        while state.need_more_shares:
            try:
                async with websockets.connect(ws_url) as tickertape:
                    while state.need_more_shares:
                        tickertape_message = json.loads(await tickertape.recv())
                        # Filter repeat tickertape messages
                        if tickertape_message.get('quote') == quote:
                            client_logger.warning('Discarding repeat tickertape')
                            continue
                        quote = tickertape_message.get('quote') or quote
                        print('target:', int(state.target or 0), 'ask:', ask, 'shares_bought:', state.shares_bought)

                        try:
                            ask = quote['ask']
                            ask_size = quote['askSize']
                        except KeyError:
                            continue

                        state.initialize_target(ask)

                        if not state.evaluate_ask(ask):
                            continue
                        if not ask_size > 0:
                            continue
                        bid_size = state.bid_size(ask_size)
                        loop.create_task(buy_and_update(bid_size, ask, state, api, web))
            except websockets.exceptions.ConnectionClosed:
                client_logger.exception('WebSocket closed. Restarting it...')
                try:
                    client_logger.exception(await web.instance(instance_id))
                except APIError:
                    client_logger.exception('Looks like the instance was shut down')
                    return

        if not state.need_more_shares:
            client_logger.info('Think we won, chief')
        await asyncio.wait(asyncio.Task.all_tasks(), timeout=10)
        await asyncio.sleep(10)
        client_logger.info(await web.instance(instance_id))
        await web.stop(instance_id)


def main():
    loop = asyncio.get_event_loop()
    loop.run_until_complete(keep_buying())


if __name__ == '__main__':
    main()
