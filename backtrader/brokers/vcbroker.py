#!/usr/bin/env python
# -*- coding: utf-8; py-indent-offset:4 -*-
###############################################################################
#
# Copyright (C) 2015-2023 Daniel Rodriguez
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
###############################################################################
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import collections
from datetime import date, datetime, timedelta
import threading

from backtrader import BrokerBase, Order, BuyOrder, SellOrder
from backtrader.comminfo import CommInfoBase
from backtrader.feed import DataBase
from backtrader.metabase import MetaParams
from backtrader.position import Position
from backtrader.utils.py3 import with_metaclass

from backtrader.stores import vcstore


class VCCommInfo(CommInfoBase):
    """
    Commissions are calculated by ib, but the trades calculations in the
    ```Strategy`` rely on the order carrying a CommInfo object attached for the
    calculation of the operation cost and value.

    These are non-critical informations, but removing them from the trade could
    break existing usage and it is better to provide a CommInfo objet which
    enables those calculations even if with approvimate values.

    The margin calculation is not a known in advance information with IB
    (margin impact can be gotten from OrderState objects) and therefore it is
    left as future exercise to get it"""

    def getvaluesize(self, size, price):
        # In real life the margin approaches the price
        return abs(size) * price

    def getoperationcost(self, size, price):
        """Returns the needed amount of cash an operation would cost"""
        # Same reasoning as above
        return abs(size) * price


class MetaVCBroker(BrokerBase.__class__):
    def __init__(cls, name, bases, dct):
        """Class has already been created ... register"""
        # Initialize the class
        super(MetaVCBroker, cls).__init__(name, bases, dct)
        vcstore.VCStore.BrokerCls = cls


class VCBroker(with_metaclass(MetaVCBroker, BrokerBase)):
    """Broker implementation for VisualChart.

    This class maps the orders/positions from VisualChart to the
    internal API of ``backtrader``.

    Params:

      - ``account`` (default: None)

        VisualChart supports several accounts simultaneously on the broker. If
        the default ``None`` is in place the 1st account in the ComTrader
        ``Accounts`` collection will be used.

        If an account name is provided, the ``Accounts`` collection will be
        checked and used if present

      - ``commission`` (default: None)

        An object will be autogenerated if no commission-scheme is passed as
        parameter

        See the notes below for further explanations

    Notes:

      - Position

        VisualChart reports "OpenPositions" updates through the ComTrader
        interface but only when the position has a "size". An update to
        indicate a position has moved to ZERO is reported by the absence of
        such position. This forces to keep accounting of the positions by
        looking at the execution events, just like the simulation broker does

      - Commission

        The ComTrader interface of VisualChart does not report commissions and
        as such the auto-generated CommissionInfo object cannot use
        non-existent commissions to properly account for them. In order to
        support commissions a ``commission`` parameter has to be passed with
        the appropriate commission schemes.

        The documentation on Commission Schemes details how to do this

      - Expiration Timing

        The ComTrader interface (or is it the comtypes module?) discards
        ``time`` information from ``datetime`` objects and expiration dates are
        always full dates.

      - Expiration Reporting

        At the moment no heuristic is in place to determine when a cancelled
        order has been cancelled due to expiration. And therefore expired
        orders are reported as cancelled.
    """
    params = (
        ('account', None),
        ('commission', None),
    )

    def __init__(self, **kwargs):
        super(VCBroker, self).__init__()

        self.store = vcstore.VCStore(**kwargs)

        # Account data
        self._acc_name = None
        self.startingcash = self.cash = 0.0
        self.startingvalue = self.value = 0.0

        # Position accounting
        self._lock_pos = threading.Lock()  # sync account updates
        self.positions = collections.defaultdict(Position)  # actual positions

        # Order storage
        self._lock_orders = threading.Lock()  # control access
        self.orderbyid = dict()  # orders by order id

        # Notifications
        self.notifs = collections.deque()

        # Dictionaries of values for order mapping
        self._otypes = {
            Order.Market: self.store.vcctmod.OT_Market,
            Order.Close: self.store.vcctmod.OT_Market,
            Order.Limit: self.store.vcctmod.OT_Limit,
            Order.Stop: self.store.vcctmod.OT_StopMarket,
            Order.StopLimit: self.store.vcctmod.OT_StopLimit,
        }

        self._osides = {
            Order.Buy: self.store.vcctmod.OS_Buy,
            Order.Sell: self.store.vcctmod.OS_Sell,
        }

        self._otrestriction = {
            Order.T_None: self.store.vcctmod.TR_NoRestriction,
            Order.T_Date: self.store.vcctmod.TR_Date,
            Order.T_Close: self.store.vcctmod.TR_CloseAuction,
            Order.T_Day: self.store.vcctmod.TR_Session,
        }

        self._ovrestriction = {
            Order.V_None: self.store.vcctmod.VR_NoRestriction,
        }

        self._futlikes = (
            self.store.vcdsmod.IT_Future, self.store.vcdsmod.IT_Option,
            self.store.vcdsmod.IT_Fund,
        )

    def start(self):
        super(VCBroker, self).start()
        self.store.start(broker=self)

    def stop(self):
        super(VCBroker, self).stop()
        self.store.stop()

    def getcash(self):
        # This call cannot block if no answer is available from ib
        return self.cash

    def getvalue(self, datas=None):
        return self.value

    def get_notification(self):
        return self.notifs.popleft()  # at leat a None is present

    def notify(self, order):
        self.notifs.append(order.clone())

    def next(self):
        self.notifs.append(None)  # mark notificatino boundary

    def getposition(self, data, clone=True):
        with self._lock_pos:
            pos = self.positions[data._tradename]
            if clone:
                return pos.clone()

        return pos

    def getcommissioninfo(self, data):
        if data._tradename in self.comminfo:
            return self.comminfo[data._tradename]

        comminfo = self.comminfo[None]
        if comminfo is not None:
            return comminfo

        stocklike = data._syminfo.Type in self._futlikes

        return VCCommInfo(mult=data._syminfo.PointValue, stocklike=stocklike)

    def _makeorder(self, ordtype, owner, data,
                   size, price=None, plimit=None,
                   exectype=None, valid=None,
                   tradeid=0, **kwargs):

        order = self.store.vcctmod.Order()
        order.Account = self._acc_name
        order.SymbolCode = data._tradename
        order.OrderType = self._otypes[exectype]
        order.OrderSide = self._osides[ordtype]

        order.VolumeRestriction = self._ovrestriction[Order.V_None]
        order.HideVolume = 0
        order.MinVolume = 0

        # order.UserName = 'danjrod'  # str(tradeid)
        # order.OrderId = 'a' * 50  # str(tradeid)
        order.UserOrderId = ''
        if tradeid:
            order.ExtendedInfo = 'TradeId {}'.format(tradeid)
        else:
            order.ExtendedInfo = ''

        order.Volume = abs(size)

        order.StopPrice = 0.0
        order.Price = 0.0
        if exectype == Order.Market:
            pass
        elif exectype == Order.Limit:
            order.Price = price or plimit  # cover naming confusion cases
        elif exectype == Order.Close:
            pass
        elif exectype == Order.Stop:
            order.StopPrice = price
        elif exectype == Order.StopLimit:
            order.StopPrice = price
            order.Price = plimit

        order.ValidDate = None
        if exectype == Order.Close:
            order.TimeRestriction = self._otrestriction[Order.T_Close]
        else:
            if valid is None:
                order.TimeRestriction = self._otrestriction[Order.T_None]
            elif isinstance(valid, (datetime, date)):
                order.TimeRestriction = self._otrestriction[Order.T_Date]
                order.ValidDate = valid
            elif isinstance(valid, (timedelta,)):
                if valid == Order.DAY:
                    order.TimeRestriction = self._otrestriction[Order.T_Day]
                else:
                    order.TimeRestriction = self._otrestriction[Order.T_Date]
                    order.ValidDate = datetime.now() + valid

            elif not self.valid:  # DAY
                order.TimeRestriction = self._otrestriction[Order.T_Day]

        # Support for custom user arguments
        for k in kwargs:
            if hasattr(order, k):
                setattr(order, k, kwargs[k])

        return order

    def submit(self, order, vcorder):
        order.submit(self)

        vco = vcorder
        oid = self.store.vcct.SendOrder(
            vco.Account, vco.SymbolCode,
            vco.OrderType, vco.OrderSide, vco.Volume, vco.Price, vco.StopPrice,
            vco.VolumeRestriction, vco.TimeRestriction,
            ValidDate=vco.ValidDate
        )

        order.vcorder = oid
        order.addcomminfo(self.getcommissioninfo(order.data))

        with self._lock_orders:
            self.orderbyid[oid] = order
        self.notify(order)
        return order

    def buy(self, owner, data,
            size, price=None, plimit=None,
            exectype=None, valid=None, tradeid=0,
            **kwargs):

        order = BuyOrder(owner=owner, data=data,
                         size=size, price=price, pricelimit=plimit,
                         exectype=exectype, valid=valid, tradeid=tradeid)

        order.addinfo(**kwargs)

        vcorder = self._makeorder(order.ordtype, owner, data, size, price,
                                  plimit, exectype, valid, tradeid,
                                  **kwargs)

        return self.submit(order, vcorder)

    def sell(self, owner, data,
             size, price=None, plimit=None,
             exectype=None, valid=None, tradeid=0,
             **kwargs):

        order = SellOrder(owner=owner, data=data,
                          size=size, price=price, pricelimit=plimit,
                          exectype=exectype, valid=valid, tradeid=tradeid)

        order.addinfo(**kwargs)

        vcorder = self._makeorder(order.ordtype, owner, data, size, price,
                                  plimit, exectype, valid, tradeid,
                                  **kwargs)

        return self.submit(order, vcorder)

    #
    # COM Events implementation
    #
    def __call__(self, trader):
        # Called to start the process, call in sub-thread. only the passed
        # trader can be used in the thread
        self.trader = trader

        for acc in trader.Accounts:
            if self.p.account is None or self.p.account == acc.Account:
                self.startingcash = self.cash = acc.Balance.Cash
                self.startingvalue = self.value = acc.Balance.NetWorth
                self._acc_name = acc.Account
                break  # found the account

        return self

    def OnChangedBalance(self, Account):
        if self._acc_name is None or self._acc_name != Account:
            return  # skip notifs for other accounts

        for acc in self.trader.Accounts:
            if acc.Account == Account:
                # Update store values
                self.cash = acc.Balance.Cash
                self.value = acc.Balance.NetWorth
                break

    def OnModifiedOrder(self, Order):
        # We are not expecting this: unless backtrader starts implementing
        # modify order method
        pass

    def OnCancelledOrder(self, Order):
        with self._lock_orders:
            try:
                border = self.orderbyid[Order.OrderId]
            except KeyError:
                return  # possibly external order

        border.cancel()
        self.notify(border)

    def OnTotalExecutedOrder(self, Order):
        self.OnExecutedOrder(Order, partial=False)

    def OnPartialExecutedOrder(self, Order):
        self.OnExecutedOrder(Order, partial=True)

    def OnExecutedOrder(self, Order, partial):
        with self._lock_orders:
            try:
                border = self.orderbyid[Order.OrderId]
            except KeyError:
                return  # possibly external order

        price = Order.Price
        size = Order.Volume
        if border.issell():
            size *= -1

        # Find position and do a real update - accounting happens here
        position = self.getposition(border.data, clone=False)
        pprice_orig = position.price
        psize, pprice, opened, closed = position.update(size, price)

        comminfo = border.comminfo
        closedvalue = comminfo.getoperationcost(closed, pprice_orig)
        closedcomm = comminfo.getcommission(closed, price)

        openedvalue = comminfo.getoperationcost(opened, price)
        openedcomm = comminfo.getcommission(opened, price)

        pnl = comminfo.profitandloss(-closed, pprice_orig, price)
        margin = comminfo.getvaluesize(size, price)

        # NOTE: No commission information available in the Trader interface
        # CHECK: Use reported time instead of last data time?
        border.execute(border.data.datetime[0],
                       size, price,
                       closed, closedvalue, closedcomm,
                       opened, openedvalue, openedcomm,
                       margin, pnl,
                       psize, pprice)  # pnl

        if partial:
            border.partial()
        else:
            border.completed()

        self.notify(border)

    def OnOrderInMarket(self, Order):
        # Other is in ther market ... therefore "accepted"
        with self._lock_orders:
            try:
                border = self.orderbyid[Order.OrderId]
            except KeyError:
                return  # possibly external order

        border.accept()
        self.notify(border)

    def OnNewOrderLocation(self, Order):
        # Can be used for "submitted", but the status is set manually
        pass

    def OnChangedOpenPositions(self, Account):
        # This would be useful if it reported a position moving back to 0. In
        # this case the report contains a no-position and this doesn't help in
        # the accounting. That's why the accounting is delegated to the
        # reception of order execution
        pass

    def OnNewClosedOperations(self, Account):
        # This call-back has not been seen
        pass

    def OnServerShutDown(self):
        pass

    def OnInternalEvent(self, p1, p2, p3):
        pass
