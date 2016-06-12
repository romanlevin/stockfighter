#!/usr/bin/env python
import time
import asyncio
import aiohttp
import aiofiles
import json
import websockets
import logging
from concurrent.futures import CancelledError
from bs4 import BeautifulSoup


BASE_URL = 'https://api.stockfighter.io/ob/api'
WEBSOCKET = 'wss://api.stockfighter.io/ob/api/ws/{account}/venues/{venue}/tickertape/stocks/{stock}'
THRESHOLD = 0.95

client_logger = logging.getLogger('aiohttp.client')
client_logger.addHandler(logging.StreamHandler())
client_logger.setLevel(logging.INFO)

async def unwrap_response(response):
    if response.status >= 400:
        client_logger.error('')
        client_logger.error('Headers: %r', response.headers)
        client_logger.error('Request url: %r', response.url)
        client_logger.error('Reason: %r', response.reason)
        client_logger.error('Response body %r', await response.text())
        raise Exception('HTTP Error: %d' % response.status)
    try:
        body = await response.json()
    except Exception:
        client_logger.error(await response.text())
        raise
    if body.get('ok') is False:
        client_logger.error('Response: %r', response)
        client_logger.error('Request url: %r', response.url)
        client_logger.error('Response body: %r', await response.text())
        raise Exception('API Error: %s' % body.get('error') or 'unspecified')
    return body


async def quote_listener(quote, account, venue, stock):
    try:
        url = WEBSOCKET.format(account=account, venue=venue, stock=stock)
        async with websockets.connect(url) as socket:
            while True:
                message = json.loads(await socket.recv())
                if 'quote' in message:
                    quote.update(message['quote'])
    except (GeneratorExit, CancelledError, RuntimeError):
        pass
    except websockets.exceptions.ConnectionClosed:
        # create a new connection
        logging.exception('WebSocket connection closed')
        loop = asyncio.get_event_loop()
        loop.create_task(quote_listener(quote, account, venue, stock))


class Web:
    levels_url = 'https://www.stockfighter.io/ui/levels'
    instance_url = 'https://www.stockfighter.io/gm/instances/{id}'
    resume_url = instance_url + '/resume'
    restart_url = instance_url + '/restart'
    stop_url = instance_url + '/stop'
    cookies = None

    def __enter__(self):
        self.session = aiohttp.ClientSession(headers={'Accept': 'application/json'}, cookies=self.cookies)
        return self

    def __exit__(self, *args):
        self.session.close()

    async def login(self):
        async with self.session.get('https://www.stockfighter.io', headers={'Accept': ''}) as response:
            body = await response.text()
        soup = BeautifulSoup(body, 'html.parser')
        csrf = soup.find_all(attrs={'name': 'csrf-token'})[0]['content']
        async with aiofiles.open('session.json') as f:
            creds = json.loads(await f.read())
        async with self.session.post('https://www.stockfighter.io/ui/login', data=creds,
                                     headers={'X-CSRF-Token': csrf,
                                              'Referer': 'https://www.stockfighter.io/'}) as response:
            token = (await unwrap_response(response))['token']
            self.cookies = self.session.cookies
            return token

    async def instance(self, instance_id):
        async with self.session.get(self.instance_url.format(id=instance_id)) as response:
            instance = await unwrap_response(response)
            client_logger.debug('Instance: %r', instance)
            return instance

    async def level(self):
        async with self.session.get(self.levels_url) as response:
            levels = await unwrap_response(response)
            client_logger.debug('Levels: %r', levels)
            return levels

    async def stop(self, instance_id):
        async with self.session.post(self.stop_url.format(id=instance_id)) as response:
            instance = await unwrap_response(response)
            client_logger.debug('Instance %d: %r', instance_id, instance)
            return instance

    async def restart(self, instance_id):
        async with self.session.post(self.restart_url.format(id=instance_id)) as response:
            instance = await unwrap_response(response)
            client_logger.debug('Instance %d: %r', instance_id, instance)
            return instance

    async def resume(self, instance_id):
        async with self.session.post(self.resume_url.format(id=instance_id)) as response:
            try:
                instance = await unwrap_response(response)
            except Exception:
                instance = await self.restart(instance_id)
            client_logger.debug('Instance %d: %r', instance_id, instance)
            return instance


class API:
    base_url = BASE_URL
    stock_url = base_url + '/venues/{venue}/stocks/{symbol}'
    quote_url = stock_url + '/quote'
    orders_url = stock_url + '/orders'
    order_url = orders_url + '/{id}'

    directions = {'buy', 'sell'}
    order_types = {'market', 'limit', 'fill-or-kill', 'immediate-or-cancel'}

    def __enter__(self):
        self.session = aiohttp.ClientSession(
            headers={'content-type': 'application/json',
                     'X-Starfighter-Authorization': self.api_key})
        return self

    def __exit__(self, *args):
        self.session.close()

    def __init__(self, api_key, account, venue, stock):
        self.api_key = api_key
        self.account = account
        self.venue = venue
        self.stock = stock

    async def order(self, direction, shares, price, order_type, account=None, venue=None, stock=None):
        assert direction in self.directions, '`direction` must be either \'buy\' or \'sell\''
        assert order_type in self.order_types, '`order_type` must be one of %r' % self.order_types
        order = {
            'account': account or self.account,
            'venue': venue or self.venue,
            'symbol': stock or self.stock,
            'qty': shares,
            'direction': direction,
            'orderType': order_type,
            'price': price,
            }
        async with self.session.post(self.orders_url.format(**order), data=json.dumps(order)) as response:
            return await unwrap_response(response)

    async def buy(self, *args, **kwargs):
        return await self.order('buy', *args, **kwargs)

    async def sell(self, *args, **kwargs):
        return await self.order('sell', *args, **kwargs)

    async def time_bounded_order(self, *args, timeout=0.5, **kwargs):
        order = await self.order(*args, **kwargs)
        await asyncio.sleep(timeout)
        return await self.cancel_order(order['id'], venue=kwargs.get('venue'), stock=kwargs.get('stock'))

    async def quote(self, venue=None, stock=None):
        order = {
            'venue': venue or self.venue,
            'symbol': stock or self.stock,
            }
        async with self.session.get(self.quote_url.format(**order)) as response:
            return await unwrap_response(response)

    async def order_status(self, order_id, venue=None, stock=None):
        order = {
            'venue': venue or self.venue,
            'symbol': stock or self.stock,
            'id': order_id,
            }
        async with self.sessions.get(self.order_url.format(**order)) as response:
            return await unwrap_response(response)

    async def cancel_order(self, order_id, venue=None, stock=None):
        order = {
            'venue': venue or self.venue,
            'symbol': stock or self.stock,
            'id': order_id
            }
        async with self.session.delete(self.order_url.format(**order)) as response:
            return await unwrap_response(response)


def parse():
    import argparse
    parser = argparse.ArgumentParser()
    return parser.parse_args()


async def keep_buying(api_key=None, target=0, to_buy=100000):
    """
    Perform a block buy of a stock.

    Quote is updated in a different task by a websocket.
    """
    quote = {}
    loop = asyncio.get_event_loop()
    web = Web()
    with web:
        api_key = await web.login()
        levels = await web.level()
        instance_id = levels['levels']['chock_a_block']['instanceId']
        try:
            await web.stop(instance_id)
        except Exception:
            pass
        instance = await web.resume(instance_id)
    account = instance['account']
    stock = instance['tickers'][0]
    venue = instance['venues'][0]
    listener = loop.create_task(quote_listener(quote=quote, account=account, venue=venue, stock=stock))
    shares_to_buy = to_buy
    shares_bought = 0
    # buy when ask is below this
    target = target
    ask = 0
    # last - last time target was adjusted
    last = time.time()
    with API(api_key=api_key, venue=venue, account=account, stock=stock) as api:
        while shares_bought < shares_to_buy:
            # Throttle orders
            await asyncio.sleep(1.0)
            print('target:', target, 'ask:', ask, 'shares_bought:', shares_bought, end='\r')
            # check the latest quote for this stock
            try:
                ask = quote['ask']
                ask_size = quote['askSize']
            except KeyError:
                continue
            # initialize target to first ask if not set
            target = target or ask * THRESHOLD
            # is the ask too high?
            if ask > target:
                # if we didn't buy any shares for a while, raise target
                now = time.time()
                if now - last > 30:
                    target *= 1.01
                    last = now
                continue
            if not ask_size > 0:
                continue
            bid_size = min(shares_to_buy - shares_bought, ask_size)
            order = await api.time_bounded_order(direction='buy', shares=bid_size, order_type='limit', price=ask)
            filled = order['totalFilled']
            shares_bought += filled
            if filled:
                with web:
                    try:
                        instance = await web.instance(instance_id)
                        info = instance['flash']['info']
                        client_logger.info('Flash: %r', info)
                    except KeyError:
                        client_logger.warning("Couldn't extrant flash info: %r", instance)
                # in case price will continue going down, lower target
                target *= THRESHOLD
            last = time.time()

    with web:
        await web.stop(instance_id)

    listener.cancel()
    loop.stop()


def main():
    loop = asyncio.get_event_loop()
    loop.run_until_complete(keep_buying())


if __name__ == '__main__':
    main()
