# -*- coding: utf-8 -*-
from datetime import datetime
import magento
import logging
import xmlrpclib
import socket

from trytond.pool import PoolMeta, Pool
from trytond.transaction import Transaction
from trytond.pyson import Eval
from trytond.model import ModelView, ModelSQL, fields
from .api import OrderConfig

__metaclass__ = PoolMeta
__all__ = ['Channel', 'MagentoTier']

MAGENTO_STATES = {
    'invisible': ~(Eval('source') == 'magento'),
    'required': Eval('source') == 'magento'
}

INVISIBLE_IF_NOT_MAGENTO = {
    'invisible': ~(Eval('source') == 'magento'),
}

logger = logging.getLogger('magento')


def batch(iterable, n=1):
    l = len(iterable)
    for ndx in range(0, l, n):
        yield iterable[ndx:min(ndx + n, l)]


class Channel:
    """
    Sale Channel model
    """
    __name__ = 'sale.channel'

    # Instance
    magento_url = fields.Char(
        "Magento Site URL", states=MAGENTO_STATES, depends=['source']
    )
    magento_api_user = fields.Char(
        "API User", states=MAGENTO_STATES, depends=['source']
    )
    magento_api_key = fields.Char(
        "API Key", states=MAGENTO_STATES, depends=['source']
    )
    magento_carriers = fields.One2Many(
        "magento.instance.carrier", "channel", "Carriers / Shipping Methods",
        states=INVISIBLE_IF_NOT_MAGENTO, depends=['source']
    )
    magento_order_prefix = fields.Char(
        'Sale Order Prefix',
        help="This helps to distinguish between orders from different channels",
        states=INVISIBLE_IF_NOT_MAGENTO, depends=['source']
    )

    # website
    magento_website_id = fields.Integer(
        'Website ID', readonly=True,
        states=INVISIBLE_IF_NOT_MAGENTO, depends=['source']
    )
    magento_website_name = fields.Char(
        'Website Name', readonly=True,
        states=INVISIBLE_IF_NOT_MAGENTO, depends=['source']
    )
    magento_website_code = fields.Char(
        'Website Code', readonly=True,
        states=INVISIBLE_IF_NOT_MAGENTO, depends=['source']
    )
    magento_root_category_id = fields.Integer(
        'Root Category ID', states=INVISIBLE_IF_NOT_MAGENTO, depends=['source']
    )
    magento_store_name = fields.Char(
        'Store Name', readonly=True, states=INVISIBLE_IF_NOT_MAGENTO,
        depends=['source']
    )
    magento_store_id = fields.Integer(
        'Store ID', readonly=True, states=INVISIBLE_IF_NOT_MAGENTO,
        depends=['source']
    )

    #: Checking this will make sure that only the done shipments which have a
    #: carrier and tracking reference are exported.
    magento_export_tracking_information = fields.Boolean(
        'Export tracking information', help='Checking this will make sure'
        ' that only the done shipments which have a carrier and tracking '
        'reference are exported. This will update carrier and tracking '
        'reference on magento for the exported shipments as well.',
        states=INVISIBLE_IF_NOT_MAGENTO, depends=['source']
    )
    magento_taxes = fields.One2Many(
        "sale.channel.magento.tax", "channel", "Taxes",
        states=INVISIBLE_IF_NOT_MAGENTO, depends=['source']
    )
    magento_price_tiers = fields.One2Many(
        'sale.channel.magento.price_tier', 'channel', 'Default Price Tiers',
        states=INVISIBLE_IF_NOT_MAGENTO, depends=['source']
    )
    product_listings = fields.One2Many(
        'product.product.channel_listing', 'channel', 'Product Listings',
    )
    magento_payment_gateways = fields.One2Many(
        'magento.instance.payment_gateway', 'channel', 'Payments',
    )

    @classmethod
    def __setup__(cls):
        """
        Setup the class before adding to pool
        """
        super(Channel, cls).__setup__()
        cls._sql_constraints += [
            (
                'unique_magento_channel',
                    'UNIQUE(magento_url, magento_website_id, magento_store_id)',
                'This store is already added'
            )
        ]
        cls._error_messages.update({
            "connection_error": "Incorrect API Settings! \n"
                "Please check and correct the API settings on channel.",
            "multiple_channels": 'Selected operation can be done only for one'
                ' channel at a time',
            'invalid_magento_channel':
                'Current channel does not belongs to Magento !'
        })
        cls._buttons.update({
            'import_magento_carriers': {
                'invisible': Eval('source') != 'magento'
            },
            'configure_magento_connection': {
                'invisible': Eval('source') != 'magento'
            }
        })

    def validate_magento_channel(self):
        """
        Make sure channel source is magento
        """
        if self.source != 'magento':
            self.raise_user_error('invalid_magento_channel')

    @classmethod
    def get_source(cls):
        """
        Get the source
        """
        res = super(Channel, cls).get_source()
        res.append(('magento', 'Magento'))
        return res

    @staticmethod
    def default_magento_order_prefix():
        """
        Sets default value for magento order prefix
        """
        return 'mag_'

    @staticmethod
    def default_magento_root_category_id():
        """
        Sets default root category id. Is set to 1, because the default
        root category is 1
        """
        return 1

    def get_taxes(self, rate):
        "Return list of tax records with the given rate"
        for mag_tax in self.magento_taxes:
            if mag_tax.tax_percent == rate:
                return list(mag_tax.taxes)
        return []

    def import_order_states(self):
        """
        Import order states for magento channel

        Downstream implementation for channel.import_order_states
        """
        if self.source != 'magento':
            return super(Channel, self).import_order_states()

        with Transaction().set_context({'current_channel': self.id}):
            # Import order states
            with OrderConfig(
                self.magento_url, self.magento_api_user,
                self.magento_api_key
            ) as order_config_api:
                order_states_data = order_config_api.get_states()
                for code, name in order_states_data.iteritems():
                    self.create_order_state(code, name)

    @classmethod
    @ModelView.button_action('magento.wizard_configure_magento')
    def configure_magento_connection(cls, channels):
        """
        Configure magento connection for current channel

        :param channels: List of active records of channels
        """
        pass

    def test_magento_connection(self):
        """
        Test magento connection and display appropriate message to user
        :param channels: Active record list of magento channels
        """
        # Make sure channel belongs to magento
        self.validate_magento_channel()

        try:
            with magento.API(
                self.magento_url, self.magento_api_user,
                self.magento_api_key
            ):
                return
        except (
            xmlrpclib.Fault, IOError, xmlrpclib.ProtocolError, socket.timeout
        ):
            self.raise_user_error("connection_error")

    @classmethod
    @ModelView.button_action('magento.wizard_import_magento_carriers')
    def import_magento_carriers(cls, channels):
        """
        Import carriers/shipping methods from magento for channels

        :param channels: Active record list of magento channels
        """
        InstanceCarrier = Pool().get('magento.instance.carrier')

        for channel in channels:
            channel.validate_magento_channel()
            with Transaction().set_context({'current_channel': channel.id}):
                with OrderConfig(
                    channel.magento_url, channel.magento_api_user,
                    channel.magento_api_key
                ) as order_config_api:
                    mag_carriers = order_config_api.get_shipping_methods()

                InstanceCarrier.create_all_using_magento_data(mag_carriers)

    @classmethod
    def get_current_magento_channel(cls):
        """Helper method to get the current magento_channel.
        """
        channel = cls.get_current_channel()

        # Make sure channel belongs to magento
        channel.validate_magento_channel()

        return channel

    def import_products(self):
        """
        Import products for this magento channel

        Downstream implementation for channel.import_products
        """
        if self.source != 'magento':
            return super(Channel, self).import_products()

        self.import_category_tree()

        with Transaction().set_context({'current_channel': self.id}):
            with magento.Product(
                self.magento_url, self.magento_api_user, self.magento_api_key
            ) as product_api:
                # TODO: Implement pagination and import each product as async
                # task
                magento_products = product_api.list()

                products = []
                for magento_product in magento_products:
                    products.append(self.import_product(magento_product['sku']))

        return products

    def import_product(self, sku, product_data=None):
        """
        Import specific product for this magento channel

        Downstream implementation for channel.import_product
        """
        Product = Pool().get('product.product')
        Listing = Pool().get('product.product.channel_listing')

        if self.source != 'magento':
            return super(Channel, self).import_product(sku, product_data)

        if not sku:
            # SKU is required can not continue
            return

        # Sanitize SKU
        sku = sku.strip()

        products = Product.search([
            ('code', '=', sku),
        ])
        listings = Listing.search([
            ('product.code', '=', sku),
            ('channel', '=', self)
        ])

        if not products or not listings:
            # Either way we need the product data from magento. Make that
            # dreaded API call.
            with magento.Product(
                self.magento_url, self.magento_api_user,
                self.magento_api_key
            ) as product_api:
                product_data = product_api.info(sku, identifierType="sku")

                # XXX: sanitize product_data, sometimes product sku may
                # contain trailing spaces
                product_data['sku'] = product_data['sku'].strip()

            # Create a product since there is no match for an existing
            # product with the SKU.
            if not products:
                product = Product.create_from(self, product_data)
            else:
                product, = products

            if not listings:
                Listing.create_from(self, product_data)
        else:
            product = products[0]

        return product

    def import_category_tree(self):
        """
        Imports the category tree and creates categories in a hierarchy same as
        that on Magento

        :param website: Active record of website
        """
        Category = Pool().get('product.category')

        self.validate_magento_channel()

        with Transaction().set_context({'current_channel': self.id}):
            with magento.Category(
                self.magento_url, self.magento_api_user,
                self.magento_api_key
            ) as category_api:
                category_tree = category_api.tree(
                    self.magento_root_category_id
                )
                Category.create_tree_using_magento_data(category_tree)

    def import_orders(self):
        """
        Downstream implementation of channel.import_orders

        :return: List of active record of sale imported
        """
        if self.source != 'magento':
            return super(Channel, self).import_orders()

        new_sales = []
        with Transaction().set_context({'current_channel': self.id}):
            order_states = self.get_order_states_to_import()
            order_states_to_import_in = map(
                lambda state: state.code, order_states
            )

            with magento.Order(
                self.magento_url, self.magento_api_user, self.magento_api_key
            ) as order_api:
                # Filter orders store_id using list()
                # then get info of each order using info()
                # and call find_or_create_using_magento_data on sale
                filter = {
                    'store_id': {'=': self.magento_store_id},
                    'state': {'in': order_states_to_import_in},
                }
                self.write([self], {
                    'last_order_import_time': datetime.utcnow()
                })
                page = 1
                has_next = True
                orders_summaries = []
                while has_next:
                    # XXX: Pagination is only available in
                    # magento extension >= 1.6.1
                    api_res = order_api.search(
                        filters=filter, limit=3000, page=page
                    )
                    has_next = api_res['hasNext']
                    page += 1
                    orders_summaries.extend(api_res['items'])

                for order_summary in orders_summaries:
                    new_sales.append(self.import_order(order_summary))
        return new_sales

    def import_order(self, order_info):
        "Downstream implementation to import sale order from magento"
        if self.source != 'magento':
            return super(Channel, self).import_order(order_info)

        Sale = Pool().get('sale.sale')

        sale = Sale.find_using_magento_data(order_info)
        if sale:
            return sale

        with Transaction().set_context({'current_channel': self.id}):
            with magento.Order(
                self.magento_url, self.magento_api_user, self.magento_api_key
            ) as order_api:
                order_data = order_api.info(order_info['increment_id'])
                return Sale.create_using_magento_data(order_data)

    @classmethod
    def export_order_status_to_magento_using_cron(cls):
        """
        Export sales orders status to magento using cron

        :param store_views: List of active record of store view
        """
        channels = cls.search([('source', '=', 'magento')])

        for channel in channels:
            channel.export_order_status()

    def export_order_status(self):
        """
        Export sale order status to magento for the current store view.
        If last export time is defined, export only those orders which are
        updated after last export time.

        :return: List of active records of sales exported
        """
        Sale = Pool().get('sale.sale')

        if self.source != 'magento':
            return super(Channel, self).export_order_status()

        exported_sales = []
        domain = [('channel', '=', self.id)]

        if self.last_order_export_time:
            domain = [
                ('write_date', '>=', self.last_order_export_time)
            ]

        sales = Sale.search(domain)

        self.last_order_export_time = datetime.utcnow()
        self.save()

        for sale in sales:
            exported_sales.append(sale.export_order_status_to_magento())

        return exported_sales

    def export_product_catalog(self):
        """
        Export the current product to the magento category corresponding to
        the given `category` under the current magento channel

        :return: Active record of product
        """
        Channel = Pool().get('sale.channel')
        Product = Pool().get('product.product')
        ModelData = Pool().get('ir.model.data')
        Category = Pool().get('product.category')

        if self.source != 'magento':
            return super(Channel, self).export_product_catalog()

        domain = [
            ('code', '!=', None),
        ]

        if self.last_product_export_time:
            domain.append(
                ('write_date', '>=', self.last_product_export_time)
            )

        products = Product.search(domain)

        self.last_product_export_time = datetime.utcnow()
        self.save()

        exported_products = []

        category = Category(
            ModelData.get_id("magento", "product_category_magento_unclassified")
        )
        for product in products:
            exported_products.append(
                product.export_product_catalog_to_magento(category)
            )
        return exported_products

    @classmethod
    def export_shipment_status_to_magento_using_cron(cls):
        """
        Export Shipment status for shipments using cron
        """
        channels = cls.search([('source', '=', 'magento')])

        for channel in channels:
            channel.export_shipment_status_to_magento()

    def export_shipment_status_to_magento(self):
        """
        Exports shipment status for shipments to magento, if they are shipped

        :return: List of active record of shipment
        """
        Shipment = Pool().get('stock.shipment.out')
        Sale = Pool().get('sale.sale')
        SaleLine = Pool().get('sale.line')

        self.validate_magento_channel()

        sale_domain = [
            ('channel', '=', self.id),
            ('shipment_state', '=', 'sent'),
            ('magento_id', '!=', None),
            ('shipments', '!=', None),
        ]

        if self.last_shipment_export_time:
            sale_domain.append(
                ('write_date', '>=', self.last_shipment_export_time)
            )

        sales = Sale.search(sale_domain)

        self.last_shipment_export_time = datetime.utcnow()
        self.save()

        updated_sales = set([])
        for sale in sales:
            # Get the increment id from the sale reference
            increment_id = sale.reference[
                len(self.magento_order_prefix): len(sale.reference)
            ]

            for shipment in sale.shipments:
                try:
                    # Some checks to make sure that only valid shipments are
                    # being exported
                    if shipment.is_tracking_exported_to_magento or \
                            shipment.state != 'done' or \
                            shipment.magento_increment_id:
                        continue
                    updated_sales.add(sale)
                    with magento.Shipment(
                        self.magento_url, self.magento_api_user,
                        self.magento_api_key
                    ) as shipment_api:
                        item_qty_map = {}
                        for move in shipment.outgoing_moves:
                            if isinstance(move.origin, SaleLine) \
                                    and move.origin.magento_id:
                                # This is done because there can be multiple
                                # lines with the same product and they need
                                # to be send as a sum of quanitities
                                item_qty_map.setdefault(
                                    str(move.origin.magento_id), 0
                                )
                                item_qty_map[str(move.origin.magento_id)] += \
                                    move.quantity
                        shipment_increment_id = shipment_api.create(
                            order_increment_id=increment_id,
                            items_qty=item_qty_map
                        )
                        Shipment.write(list(sale.shipments), {
                            'magento_increment_id': shipment_increment_id,
                        })

                        if self.magento_export_tracking_information and (
                            hasattr(shipment, 'tracking_number') and
                            hasattr(shipment, 'carrier') and
                            shipment.tracking_number and shipment.carrier
                        ):
                            with Transaction().set_context(
                                    current_channel=self.id):
                                shipment.export_tracking_info_to_magento()
                except xmlrpclib.Fault, fault:
                    if fault.faultCode == 102:
                        # A shipment already exists for this order,
                        # we cannot do anything about it.
                        # Maybe it was already exported earlier or was created
                        # separately on magento
                        # Hence, just continue
                        continue

        return updated_sales

    def export_product_prices(self):
        """
        Exports tier prices of products from tryton to magento for this channel
        :return: List of products
        """
        if self.source != 'magento':
            return super(Channel, self).export_product_prices()

        ChannelListing = Pool().get('product.product.channel_listing')

        price_domain = [
            ('channel', '=', self.id),
        ]

        if self.last_product_price_export_time:
            price_domain.append([
                'OR', [(
                    'product.write_date', '>=',
                    self.last_product_price_export_time
                )], [(
                    'product.template.write_date', '>=',
                    self.last_product_price_export_time
                )]
            ])

        product_listings = ChannelListing.search(price_domain)

        self.last_product_price_export_time = datetime.utcnow()
        self.save()

        for listing in product_listings:

            # Get the price tiers from the product listing if the list has
            # price tiers else get the default price tiers from current
            # channel
            price_tiers = listing.price_tiers or self.magento_price_tiers

            price_data = []
            for tier in price_tiers:
                if hasattr(tier, 'product_listing'):
                    # The price tier comes from a product listing, then it has a
                    # function field for price, we use it directly
                    price = tier.price
                else:
                    # The price tier comes from the default tiers on
                    # channel,
                    # we dont have a product on tier, so we use the current
                    # product in loop for computing the price for this tier
                    price = self.price_list.compute(
                        None, listing.product, listing.product.list_price,
                        tier.quantity, self.default_uom
                    )

                price_data.append({
                    'qty': tier.quantity,
                    'price': float(price),
                })

            # Update stock information to magento
            with magento.ProductTierPrice(
                self.magento_url, self.magento_api_user, self.magento_api_key
            ) as tier_price_api:
                tier_price_api.update(
                    listing.product_identifier, price_data,
                    identifierType="productID"
                )

        return len(product_listings)

    def get_default_tryton_action(self, code, name):
        """
        Returns tryton order state for magento state

        :param name: Name of the magento state
        :return: A dictionary of tryton state and shipment and invoice methods
        """
        if self.source != 'magento':
            return super(Channel, self).get_default_tryton_action(code, name)

        if code in ('new', 'holded'):
            return {
                'action': 'process_manually',
                'invoice_method': 'order',
                'shipment_method': 'order'
            }
        elif code in ('pending_payment', 'payment_review'):
            return {
                'action': 'import_as_past',
                'invoice_method': 'order',
                'shipment_method': 'invoice'
            }

        elif code in ('closed', 'complete'):
            return {
                'action': 'import_as_past',
                'invoice_method': 'order',
                'shipment_method': 'order'
            }

        elif code == 'processing':
            return {
                'action': 'process_automatically',
                'invoice_method': 'order',
                'shipment_method': 'order'
            }
        else:
            return {
                'action': 'do_not_import',
                'invoice_method': 'manual',
                'shipment_method': 'manual'
            }

    def update_order_status(self):
        "Downstream implementation of order_status update"
        Sale = Pool().get('sale.sale')

        if self.source != 'magento':
            return super(Channel, self).update_order_status()

        sales = Sale.search([
            ('channel', '=', self.id),
            ('state', 'in', ('confirmed', 'processing')),
        ])
        order_ids = [sale.reference for sale in sales]
        for order_ids_batch in batch(order_ids, 50):
            with magento.Order(
                self.magento_url, self.magento_api_user, self.magento_api_key
            ) as order_api:
                orders_data = order_api.info_multi(order_ids_batch)

            for i, order_data in enumerate(orders_data):
                if order_data.get('isFault'):
                    if order_data['faultCode'] == '100':
                        # 100: Requested order not exists.
                        # TODO: Remove order from channel or add some
                        # exception.
                        pass
                    logger.warning("Order %s: %s %s" % (
                        order_ids_batch[i], order_data['faultCode'],
                        order_data['faultMessage']
                    ))
                    continue
                sale, = Sale.search([
                    ('reference', '=', order_data['increment_id'])
                ])
                sale.update_order_status_from_magento(order_data=order_data)


class MagentoTier(ModelSQL, ModelView):
    """Price Tiers for store

    This model stores the default price tiers to be used while sending
    tier prices for a product from Tryton to Magento.
    The product also has a similar table like this. If there are no entries in
    the table on product, then these tiers are used.
    """
    __name__ = 'sale.channel.magento.price_tier'

    channel = fields.Many2One(
        'sale.channel', 'Magento Store', required=True, readonly=True,
        domain=[('source', '=', 'magento')]
    )
    quantity = fields.Float('Quantity', required=True)

    @classmethod
    def __setup__(cls):
        """
        Setup the class before adding to pool
        """
        super(MagentoTier, cls).__setup__()
        cls._sql_constraints += [
            (
                'channel_quantity_unique', 'UNIQUE(channel, quantity)',
                'Quantity in price tiers must be unique for a channel'
            )
        ]
