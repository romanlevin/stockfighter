#!/usr/bin/env python
import requests
import os
import time
import pprint

BASE_URL = 'https://api.stockfighter.io/ob/api'


class API:
    base_url = BASE_URL
    stock_url = base_url + '/venues/{venue}/stocks/{symbol}'
    quote_url = stock_url + '/quote'
    orders_url = stock_url + '/orders'
    order_url = orders_url + '/{id}'

    directions = {'buy', 'sell'}
    order_types = {'market', 'limit', 'fill-or-kill', 'immediate-or-cancel'}

    class StockFighterAuth(requests.auth.AuthBase):
        def __init__(self, api_key):
            self.api_key = api_key

        def __call__(self, r):
            r.headers['X-Starfighter-Authorization'] = self.api_key
            return r

    def __init__(self, api_key=None, account=None, venue=None, stock=None):
        api_key = api_key or os.getenv('apikey')
        if not api_key:
            raise ValueError('No API key set')

        auth = self.StockFighterAuth(api_key)
        self.session = requests.Session()
        self.session.auth = auth

        # Default values
        self.account = account or os.getenv('account')
        self.venue = venue or os.getenv('venue')
        self.stock = stock or os.getenv('stock')

    def order(self, direction, shares, price, order_type, account=None, venue=None, stock=None):
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
        response = self.session.post(
                self.orders_url.format(**order),
                json=order)
        response.raise_for_status()
        body = response.json()
        if body.get('ok') is False:
            pprint.pprint(response.request.body)
            pprint.pprint(response.request.headers)
            raise Exception(body.get('error') or response)
        return response.json()

    def buy(self, *args, **kwargs):
        return self.order('buy', *args, **kwargs)

    def sell(self, *args, **kwargs):
        return self.order('sell', *args, **kwargs)

    def time_bounded_order(self, *args, timeout=0.5, **kwargs):
        order = self.order(*args, **kwargs)
        # pprint.pprint(order)
        time.sleep(timeout)
        return self.cancel_order(order['id'], venue=kwargs.get('venue'), stock=kwargs.get('stock'))

    def quote(self, venue=None, stock=None):
        order = {
            'venue': venue or self.venue,
            'symbol': stock or self.stock
            }
        response = self.session.get(self.quote_url.format(**order))
        response.raise_for_status()
        # pprint.pprint(response.json())
        return response.json()

    def order_status(self, order_id, venue=None, stock=None):
        order = {
            'venue': venue or self.venue,
            'symbol': stock or self.stock,
            'id': order_id,
            }
        response = self.sessions.get(self.order_url.format(**order))
        response.raise_for_status()
        return response.json()

    def cancel_order(self, order_id, venue=None, stock=None):
        order = {
            'venue': venue or self.venue,
            'symbol': stock or self.stock,
            'id': order_id
            }
        response = self.session.delete(self.order_url.format(**order))
        response.raise_for_status()
        return response.json()


def parse():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('-k', '--apikey', help='API Key')
    parser.add_argument('-v', '--venue', help='The stock exchange')
    parser.add_argument('-a', '--account', help='The account')
    parser.add_argument('-s', '--stock', help='The stock in question')
    parser.add_argument('-t', '--target', help='The stock in question', type=int)
    parser.add_argument('to_buy', help='Amount of shares to buy', type=int)
    return parser.parse_args()


def main():
    args = parse()
    api = API(api_key=args.apikey, venue=args.venue, account=args.account, stock=args.stock)
    shares_to_buy = args.to_buy
    shares_bought = 0
    first_ask = 0
    if args.target:
        first_ask = int(args.target / 0.95)
    ask = 0
    while shares_bought < shares_to_buy:
        print(
                'first_ask:', first_ask, 'last_ask:', ask, 'waiting_for:',
                first_ask * 0.97, 'shares_bought:', shares_bought, end='\r')
        quote = api.quote()
        try:
            ask = quote['ask']
            ask_size = quote['askSize']
        except KeyError:
            continue
        first_ask = first_ask or ask
        if ask > first_ask * 0.95:
            time.sleep(1.2)
            continue
        bid_size = min(shares_to_buy - shares_bought, ask_size)
        order = api.time_bounded_order(direction='buy', shares=bid_size, order_type='limit', price=ask)
        # pprint.pprint(order)
        filled = order['totalFilled']
        shares_bought += filled
        # pprint.pprint(order)
        print('Shares bought:', shares_bought)


if __name__ == '__main__':
    main()
