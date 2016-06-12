#!/usr/bin/env python
import time
import os
import asyncio
import aiohttp
import json
import websockets


BASE_URL = 'https://api.stockfighter.io/ob/api'
WEBSOCKET = 'wss://api.stockfighter.io/ob/api/ws/{account}/venues/{venue}/tickertape/stocks/{stock}'
THRESHOLD = 0.95


async def unwrap_response(response):
    if response.status >= 400:
        raise Exception('HTTP Error: %d' % response.status)
    body = await response.json()
    if body.get('ok') is False:
        raise Exception('API Error: %s' % body.get('error') or 'unspecified')
    return body


async def quote_listener(args, quote):
    url = WEBSOCKET.format(**vars(args))
    try:
        async with websockets.connect(url) as socket:
            while True:
                try:
                    message = json.loads(await socket.recv())
                except GeneratorExit:
                    break
                if 'quote' in message:
                    quote.update(message['quote'])
    except websockets.exceptions.ConnectionClosed:
        # create a new connection
        loop = asyncio.get_event_loop()
        loop.create_task(quote_listener(args, quote))


class API:
    base_url = BASE_URL
    stock_url = base_url + '/venues/{venue}/stocks/{symbol}'
    quote_url = stock_url + '/quote'
    orders_url = stock_url + '/orders'
    order_url = orders_url + '/{id}'

    directions = {'buy', 'sell'}
    order_types = {'market', 'limit', 'fill-or-kill', 'immediate-or-cancel'}

    def __init__(self, api_key=None, account=None, venue=None, stock=None):
        api_key = api_key or os.getenv('apikey')
        if not api_key:
            raise ValueError('No API key set')

        self.session = aiohttp.ClientSession(
            headers={'content-type': 'application/json',
                     'X-Starfighter-Authorization': api_key})

        # Default values
        self.account = account or os.getenv('account')
        self.venue = venue or os.getenv('venue')
        self.stock = stock or os.getenv('stock')

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
    parser.add_argument('-k', '--apikey', help='API Key')
    parser.add_argument('-v', '--venue', help='The stock exchange')
    parser.add_argument('-a', '--account', help='The account')
    parser.add_argument('-s', '--stock', help='The stock in question')
    parser.add_argument('-t', '--target', help='The stock in question', type=int, default=0)
    parser.add_argument('to_buy', help='Amount of shares to buy', type=int)
    return parser.parse_args()


async def keep_buying(quote, apikey=None, target=0, venue=None, account=None, stock=None, to_buy=None):
    """
    Perform a block buy of a stock.

    Quote is updated in a different task by a websocket.
    """
    assert to_buy
    api = API(api_key=apikey, venue=venue, account=account, stock=stock)
    shares_to_buy = to_buy
    shares_bought = 0
    # buy when ask is below this
    target = target
    ask = 0
    # last - last time target was adjusted
    last = time.time()
    with api.session:
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
                if now - last > 10:
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
                # in case price will continue going down, lower target
                target *= 0.99
            last = time.time()


def main():
    args = parse()
    loop = asyncio.get_event_loop()
    quote = {}
    try:
        loop.create_task(quote_listener(args, quote))
        loop.run_until_complete(keep_buying(quote=quote, **vars(args)))
    finally:
        loop.close()


if __name__ == '__main__':
    main()
