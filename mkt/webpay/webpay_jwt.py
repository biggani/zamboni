import calendar
import time
import uuid
from urllib import urlencode

from django.conf import settings
from django.utils.functional import cached_property

import commonware.log

import amo
from amo.helpers import absolutify
from amo.urlresolvers import reverse
from lib.crypto.webpay import sign_webpay_jwt
from mkt.webpay.utils import strip_tags, make_external_id
from stats.models import Contribution

log = commonware.log.getLogger('z.purchase')


def get_product_jwt(product, user=None, region=None,
                    source=None, lang=None, client_data=None):
    """Prepare a JWT for paid products to pass into navigator.pay()"""

    # TODO: Contribution should be created outside of the JWT producer
    contribution = Contribution.objects.create(
        addon_id=product.addon().pk,
        amount=product.amount(region),
        client_data=client_data,
        paykey=None,
        price_tier=product.price(),
        source=source,
        source_locale=lang,
        type=amo.CONTRIB_PENDING,
        user=user,
        uuid=str(uuid.uuid4()),
    )

    log.debug('Storing contrib for uuid: {0}'.format(contribution.uuid))

    user_id = user.pk if user else None
    log.debug('Starting purchase of app: {0} by user: {1}'.format(
        product.id(), user_id))

    issued_at = calendar.timegm(time.gmtime())

    token_data = {
        'iss': settings.APP_PURCHASE_KEY,
        'typ': settings.APP_PURCHASE_TYP,
        'aud': settings.APP_PURCHASE_AUD,
        'iat': issued_at,
        'exp': issued_at + 3600,  # expires in 1 hour
        'request': {
            'id': product.external_id(),
            'name': unicode(product.name()),
            'icons': product.icons(),
            'description': strip_tags(product.description()),
            'pricePoint': product.price().name,
            'productData': urlencode(product.product_data(contribution)),
            'chargebackURL': absolutify(reverse('webpay.chargeback')),
            'postbackURL': absolutify(reverse('webpay.postback')),
        }
    }

    token = sign_webpay_jwt(token_data)

    log.debug('Preparing webpay JWT for self.product {0}: {1}'.format(
        product.id(), token))

    return {
        'webpayJWT': token,
        'contribStatusURL': reverse(
            'webpay-status',
            kwargs={
                'uuid': contribution.uuid
            }
        )
    }


class WebAppProduct(object):
    """Binding layer to pass a web app into a JWT producer"""

    def __init__(self, webapp):
        self.webapp = webapp

    def id(self):
        return self.webapp.pk

    def external_id(self):
        return make_external_id(self.webapp)

    def name(self):
        return self.webapp.name

    def addon(self):
        return self.webapp

    def amount(self, region):
        return self.webapp.get_price(region=region.id)

    def price(self):
        return self.webapp.premium.price

    def icons(self):
        icons = {}
        for size in amo.ADDON_ICON_SIZES:
            icons[str(size)] = absolutify(self.webapp.get_icon_url(size))

        return icons

    def description(self):
        return self.webapp.description

    def application_size(self):
        return self.webapp.current_version.all_files[0].size

    @cached_property
    def payment_account(self):
        return (self.webapp
                    .single_pay_account()
                    .payment_account)

    def seller_uuid(self):
        return self.payment_account.solitude_seller.uuid

    def public_id(self):
        return self.webapp.get_or_create_public_id()

    def product_data(self, contribution):
        return {
            'addon_id': self.webapp.pk,
            'application_size': self.application_size(),
            'contrib_uuid': contribution.uuid,
            'seller_uuid': self.seller_uuid(),
            'public_id': self.public_id(),
        }


class InAppProduct(object):
    """Binding layer to pass a in app object into a JWT producer"""

    def __init__(self, inapp):
        self.inapp = inapp

    def id(self):
        return self.inapp.pk

    def external_id(self):
        return 'inapp.{0}'.format(make_external_id(self.inapp))

    def name(self):
        return self.inapp.name

    def addon(self):
        return self.inapp.webapp

    def amount(self, region):
        # In app payments are unauthenticated so we have no user
        # and therefore can't determine a meaningful region
        return None

    def price(self):
        return self.inapp.price

    def icons(self):
        # TODO: Default to 64x64 icon until addressed in
        # https://bugzilla.mozilla.org/show_bug.cgi?id=981093
        return {64: absolutify(self.inapp.logo_url)}

    def description(self):
        return self.inapp.webapp.description

    def application_size(self):
        # TODO: Should this be not none, and if so
        # How do we determine the size of an in app object?
        return None

    def seller_uuid(self):
        return (self.inapp
                    .webapp
                    .single_pay_account()
                    .payment_account
                    .solitude_seller
                    .uuid)

    def product_data(self, contribution):
        return {
            'addon_id': self.inapp.webapp.pk,
            'inapp_id': self.inapp.pk,
            'application_size': self.application_size(),
            'contrib_uuid': contribution.uuid,
            'seller_uuid': self.seller_uuid(),
        }
