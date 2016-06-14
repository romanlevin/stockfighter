import aiohttp
import aiofiles
from bs4 import BeautifulSoup
import logging
import json
import asyncio
import websockets

client_logger = logging.getLogger('aiohttp.client')

BASE_URL = 'https://api.stockfighter.io/ob/api'
WEBSOCKET = 'wss://api.stockfighter.io/ob/api/ws/{account}/venues/{venue}/tickertape/stocks/{stock}'


class HTTPError(Exception):
    pass


class APIError(HTTPError):
    pass


class WebsocketConnectionClosed(websockets.exceptions.ConnectionClosed):
    pass

async def unwrap_response(response):
    if response.status >= 400:
        raise HTTPError('HTTP Error: %d' % response.status, response)
    try:
        body = await response.json()
    except Exception:
        raise APIError('Could not extract json body: %r' % await response.text(), response)
    if body.get('ok') is False:
        raise APIError('API Error: %s' % body.get('error') or 'unspecified')
    return body


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
        client_logger.info('Logging in')
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
            client_logger.info('Logged in with token %r', token)
            return token

    async def instance(self, instance_id):
        client_logger.info('Getting instance data')
        async with self.session.get(self.instance_url.format(id=instance_id)) as response:
            instance = await unwrap_response(response)
            client_logger.debug('Instance: %r', instance)
            return instance

    async def level(self):
        client_logger.info('Getting levels')
        async with self.session.get(self.levels_url) as response:
            levels = await unwrap_response(response)
            client_logger.debug('Levels: %r', levels)
            return levels

    async def stop(self, instance_id):
        client_logger.info('Stopping instance %d', instance_id)
        async with self.session.post(self.stop_url.format(id=instance_id)) as response:
            instance = await unwrap_response(response)
            client_logger.debug('Instance %d: %r', instance_id, instance)
            return instance

    async def restart(self, instance_id):
        client_logger.info('Restarting instance %d', instance_id)
        async with self.session.post(self.restart_url.format(id=instance_id)) as response:
            instance = await unwrap_response(response)
            client_logger.debug('Instance %d: %r', instance_id, instance)
            return instance

    async def resume(self, instance_id):
        client_logger.info('Resume instance %d', instance_id)
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
        client_logger.info('Making a %s order for %d shares at $%.2f', direction, shares, price / 100)
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
        client_logger.info('Cancelling order %d', order_id)
        order = {
            'venue': venue or self.venue,
            'symbol': stock or self.stock,
            'id': order_id
            }
        async with self.session.delete(self.order_url.format(**order)) as response:
            return await unwrap_response(response)

    class WebsocketManager:
        def __init__(self, url):
            self.url = url
            self.socket = None

        async def __aenter__(self):
            return await self()

        async def __aexit__(self, *args):
            await self.socket.close()

        async def __call__(self):
            self.socket = await websockets.connect(self.url)
            return self.socket

    def stock_tickertape(self, account, venue, stock):
        url = WEBSOCKET.format(account=account, venue=venue, stock=stock)
        return self.WebsocketManager(url)
