from typing import List

import numpy as np
from numba import typeof, types
from numba.experimental import jitclass

from bots.base_bot import Bot, base_bot_spec
from definitions.candle import Candle, empty_candle_list
from definitions.order import Order, LONG, SHORT, CANCELED, NEW, MARKET, LIMIT, FILLED, PARTIALLY_FILLED, TP, SL, LQ, \
    CALCULATED, SELL, BUY, empty_order_list
from definitions.order_list import OrderList
from definitions.position import Position
from helpers.optimized import calculate_available_margin, quantity_to_cost, calculate_long_pnl, calculate_short_pnl, \
    round_down, calculate_new_position_size_position_price, calculate_bankruptcy_price


@jitclass([
    ('quantity_step', types.float64),
    ('price_step', types.float64),
    ('call_interval', types.float64),
    ('leverage', types.float64),
    ('symbol', types.string),
    ('maker_fee', types.float64),
    ('taker_fee', types.float64),
    ('latency', types.float64)
])
class BacktestConfig:
    """
    A class representing a backtest config.
    """

    def __init__(self, quantity_step: float, price_step: float, call_interval: float, leverage: float, symbol: str,
                 maker_fee: float, taker_fee: float, latency: float):
        """
        Creates a backtest config.
        :param quantity_step: Quantity step to use in backtesting.
        :param price_step: Price step to use in backtesting.
        :param call_interval: Call interval for strategy to use in backtesting.
        :param leverage: Leverage to use in backtesting.
        :param symbol: The symbol to test.
        :param maker_fee: The maker fee to use.
        :param taker_fee: The taker fee to use.
        :param latency: The latency to use.
        """
        self.quantity_step = quantity_step
        self.price_step = price_step
        self.call_interval = call_interval
        self.leverage = leverage
        self.symbol = symbol
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.latency = latency


@jitclass(base_bot_spec +
          [
              ("config", typeof(BacktestConfig(0.0, 0.0, 1.0, 1.0))),
              ("strategy", typeof(to_be_replaced_strategy)),
              ("orders_to_execute", typeof(OrderList())),
              ("data", types.float64[:, :]),
              ("current_timestamp", types.int64),
              ("latency", types.float64)
          ])
# ToDo:
#  Add list of fills after each order fill.
#  Add (hourly) statistics.
class BacktestBot(Bot):
    """
    A class to backtest a strategy. Can not be directly imported due to dynamically loading the correct strategy
    definition into the numba jitclass definition.
    """
    __init_Base = Bot.__init__

    def __init__(self, config: BacktestConfig, strategy, data: np.ndarray):
        """
        Creates an instance of the backtest bot with configuration, strategy, and data.
        :param config: A backtest configuration class.
        :param strategy: A strategy implementing the logic.
        :param data: The data consisting of timestamp, open, high, low, close, and volume candles.
        """
        self.__init_Base()
        self.config = config
        self.strategy = strategy
        self.orders_to_execute = OrderList()
        self.data = data
        self.current_timestamp = 0
        self.latency = config.latency
        self.quantity_step = config.quantity_step
        self.price_step = config.price_step
        self.call_interval = config.call_interval
        self.leverage = config.leverage
        self.symbol = config.symbol
        self.maker_fee = config.maker_fee
        self.taker_fee = config.taker_fee

    def execute_exchange_logic(self, last_candle: Candle) -> bool:
        """
        Executes the exchange logic after each candle.
        First, checks if the account got liquidated.
        Second, checks each open order whether it was executed or not. If it was executed, triggers order and position
        updates.
        Third, checks which of the created orders arrived at the exchange and are added to the open orders.
        :param last_candle: The candle to use for the checks.
        :return: Whether the account can continue.
        """
        # ToDo:
        #  Include min notional and min quantity checks for each order.
        # Check if the positions got liquidated in the last candle
        if calculate_available_margin(self.get_balance(), self.get_position().long.size,
                                      self.get_position().long.price, self.get_position().short.size,
                                      self.get_position().short.price, last_candle.close, False,
                                      1, self.leverage) <= 0.0:
            if self.get_position().long.size != 0.0:
                order = Order(self.symbol, 0, last_candle.close, last_candle.close, self.get_position().long.size,
                              CALCULATED, SELL, self.current_timestamp, LQ, LONG)
                self.handle_order_update(order)
                self.handle_account_update(0.0, Position(self.symbol, 0.0, 0.0, 0.0, 0.0, self.leverage, LONG),
                                           self.get_position().short)
            if self.get_position().short.size != 0.0:
                order = Order(self.symbol, 0, last_candle.close, last_candle.close, self.get_position().short.size,
                              CALCULATED, SELL, self.current_timestamp, LQ, SHORT)
                self.handle_order_update(order)
                self.handle_account_update(0.0, self.get_position().long,
                                           Position(self.symbol, 0.0, 0.0, 0.0, 0.0, self.leverage, SHORT))
            return False

        orders_to_remove = empty_order_list()
        # Check which long orders where triggered in the last candle
        for order in self.open_orders.long:
            execution = False
            o = order.copy()
            if order.type == MARKET:
                # Market types take the average price of the last candle
                execution = True
                o.price = round_down(
                    np.average([last_candle.open, last_candle.high, last_candle.low, last_candle.close]),
                    self.price_step)
            if last_candle.low < order.price:
                if (order.type == LIMIT and order.side == BUY) or order.type == SL:
                    # Limit buy orders and stop loss are treated the same way
                    execution = True
            if last_candle.high > order.price:
                if (order.type == LIMIT and order.side == SELL) or order.type == TP:
                    # Limit sell orders and take profit are treated the same way
                    execution = True
            if execution:
                if last_candle.volume >= order.qty:
                    o.action = FILLED
                    orders_to_remove.append(order)
                else:
                    # Partial fills update the quantity of the order
                    o.action = PARTIALLY_FILLED
                    o.qty = o.qty - last_candle.volume
                    order.qty = order.qty - last_candle.volume
                self.handle_order_update(o)
                p = self.get_position().long.copy()
                if order.type == MARKET:
                    fee_paid = -quantity_to_cost(o.qty, o.price, False, 1.0) * self.taker_fee
                else:
                    fee_paid = -quantity_to_cost(o.qty, o.price, False, 1.0) * self.maker_fee
                if order.side == SELL:
                    pnl = calculate_long_pnl(self.get_position().long.price, o.price,
                                             o.qty if o.action == FILLED else last_candle.volume, False, 1.0)
                    # Calculate size and price with negative quantity
                    p.qty, p.price = calculate_new_position_size_position_price(p.qty, p.price, -(
                        o.qty if o.action == FILLED else last_candle.volume), o.price, self.quantity_step)
                else:
                    p.qty, p.price = calculate_new_position_size_position_price(p.qty, p.price, (
                        o.qty if o.action == FILLED else last_candle.volume), o.price, self.quantity_step)
                    pnl = 0.0

                p.leverage = self.leverage
                p.position_side = LONG
                p.liquidation_price = calculate_bankruptcy_price(self.get_balance() + fee_paid + pnl, p.qty, p.price,
                                                                 self.get_position().short.qty,
                                                                 self.get_position().short.price, False, 1.0)
                self.handle_account_update(self.get_balance() + fee_paid + pnl, p, self.get_position().short)

        self.open_orders.delete_long(orders_to_remove)
        orders_to_remove = empty_order_list()
        # Check which short orders where triggered in the last candle
        for order in self.open_orders.short:
            execution = False
            o = order.copy()
            if order.type == MARKET:
                # Market types take the average price of the last candle
                execution = True
                o.price = round_down(
                    np.average([last_candle.open, last_candle.high, last_candle.low, last_candle.close]),
                    self.price_step)
            if last_candle.high > order.price:
                if (order.type == LIMIT and order.side == BUY) or order.type == SL:
                    # Limit buy orders and stop loss are treated the same way
                    execution = True
            if last_candle.low < order.price:
                if (order.type == LIMIT and order.side == SELL) or order.type == TP:
                    # Limit sell orders and take profit are treated the same way
                    execution = True
            if execution:
                if last_candle.volume >= order.qty:
                    o.action = FILLED
                    orders_to_remove.append(order)
                else:
                    # Partial fills update the quantity of the order
                    o.action = PARTIALLY_FILLED
                    o.qty = o.qty - last_candle.volume
                    order.qty = order.qty - last_candle.volume
                self.handle_order_update(o)
                p = self.get_position().short.copy()
                if order.type == MARKET:
                    fee_paid = -quantity_to_cost(o.qty, o.price, False, 1.0) * self.taker_fee
                else:
                    fee_paid = -quantity_to_cost(o.qty, o.price, False, 1.0) * self.maker_fee
                if order.side == SELL:
                    pnl = calculate_short_pnl(self.get_position().short.price, o.price,
                                              o.qty if o.action == FILLED else last_candle.volume, False, 1.0)
                    # Calculate size and price with negative quantity
                    p.qty, p.price = calculate_new_position_size_position_price(p.qty, p.price, -(
                        o.qty if o.action == FILLED else last_candle.volume), o.price, self.quantity_step)
                else:
                    p.qty, p.price = calculate_new_position_size_position_price(p.qty, p.price, (
                        o.qty if o.action == FILLED else last_candle.volume), o.price, self.quantity_step)
                    pnl = 0.0

                p.leverage = self.leverage
                p.position_side = LONG
                p.liquidation_price = calculate_bankruptcy_price(self.get_balance() + fee_paid + pnl,
                                                                 self.get_position().long.qty,
                                                                 self.get_position().long.price, p.qty, p.price, False,
                                                                 1.0)
                self.handle_account_update(self.get_balance() + fee_paid + pnl, self.get_position().long, p)

        self.open_orders.delete_long(orders_to_remove)
        orders_to_remove = empty_order_list()
        # Check which long orders arrived at the exchange and where added to the open orders
        for order in self.orders_to_execute.long:
            if order.timestamp + self.latency <= self.current_timestamp:
                if order.qty * order.price < calculate_available_margin(self.get_balance(),
                                                                        self.get_position().long.size,
                                                                        self.get_position().long.price,
                                                                        self.get_position().short.size,
                                                                        self.get_position().short.price,
                                                                        last_candle.close,
                                                                        False, 1, self.leverage):
                    self.handle_order_update(order.copy())
                orders_to_remove.append(order)

        self.orders_to_execute.delete_long(orders_to_remove)
        orders_to_remove = empty_order_list()
        # Check which short orders arrived at the exchange and where added to the open orders
        for order in self.orders_to_execute.short:
            if order.timestamp + self.latency <= self.current_timestamp:
                if order.qty * order.price < calculate_available_margin(self.get_balance(),
                                                                        self.get_position().long.size,
                                                                        self.get_position().long.price,
                                                                        self.get_position().short.size,
                                                                        self.get_position().short.price,
                                                                        last_candle.close,
                                                                        False, 1, self.leverage):
                    self.handle_order_update(order.copy())
                orders_to_remove.append(order)

        self.orders_to_execute.delete_short(orders_to_remove)
        return True

    def prepare_candle(self, row: np.ndarray) -> Candle:
        """
        Converts a row of data into a candle object.
        :param row: The row to convert.
        :return: A candle object.
        """
        candle = Candle(row[1], row[2], row[3], row[4], row[5])
        return candle

    def start_websocket(self) -> None:
        """
        Executes the iteration over the provided data. Triggers updating of sent orders, open orders, position, and
        balance after each candle tick. Also executes the strategy decision logic after the specified call interval.
        :return:
        """
        price_list = empty_candle_list()
        last_update = self.data[0, 0]
        # Time, open, high, low, close, qty
        for i in self.data:
            self.current_timestamp = i[0]
            candle = self.prepare_candle(i)
            cont = self.execute_exchange_logic(candle)
            if not cont:
                return
            price_list.append(candle)
            if self.current_timestamp - last_update >= self.strategy.call_interval * 1000:
                last_update = self.current_timestamp
                self.decide(price_list)
                price_list = empty_candle_list()

    def create_orders(self, orders_to_create: List[Order]):
        """
        Adds the order to the ones waiting for the exchange to accept. Also corrects the precision and sets the
        timestamp and action. This is for new orders.
        :param orders_to_create: A list of orders to submit to the exchange.
        :return:
        """
        long_add = empty_order_list()
        short_add = empty_order_list()
        for order in orders_to_create:
            order.symbol = self.symbol
            order.timestamp = self.current_timestamp
            order.action = NEW
            if order.position_side == LONG:
                long_add.append(order)
            elif order.position_side == SHORT:
                short_add.append(order)
        self.orders_to_execute.add_long(long_add)
        self.orders_to_execute.add_short(short_add)

    def cancel_orders(self, orders_to_cancel: List[Order]):
        """
        Adds the order to the ones waiting for the exchange to accept. Also corrects the precision and sets the
        timestamp and action.This is for order cancellations.
        :param orders_to_cancel: A list of orders to submit to the exchange.
        :return:
        """
        long_delete = empty_order_list()
        short_delete = empty_order_list()
        for order in orders_to_cancel:
            order.symbol = self.symbol
            order.timestamp = self.current_timestamp
            order.action = CANCELED
            if order.position_side == LONG:
                long_delete.append(order)
            elif order.position_side == SHORT:
                short_delete.append(order)
        self.orders_to_execute.add_long(long_delete)
        self.orders_to_execute.add_short(short_delete)
