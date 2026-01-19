from decimal import Decimal
from datetime import datetime
from utils.constants import KENYAN_TAX_RATES


class TaxCalculator:
    @staticmethod
    def calculate_vat(amount, tax_rate=16.0, is_inclusive=False):
        """Calculate VAT amount"""
        tax_rate = Decimal(str(tax_rate))
        amount = Decimal(str(amount))
        
        if is_inclusive:
            # VAT is included in the amount
            vat_amount = (amount * tax_rate) / (100 + tax_rate)
            base_amount = amount - vat_amount
        else:
            # VAT is added to the base amount
            vat_amount = (amount * tax_rate) / 100
            base_amount = amount
        
        return {
            'base_amount': base_amount.quantize(Decimal('0.01')),
            'vat_amount': vat_amount.quantize(Decimal('0.01')),
            'total_amount': (base_amount + vat_amount).quantize(Decimal('0.01'))
        }

    @staticmethod
    def calculate_withholding_tax(amount, customer_type='INDIVIDUAL'):
        """Calculate Withholding Tax (WHT) for Kenya"""
        # WHT rates in Kenya (as of 2024)
        wht_rates = {
            'INDIVIDUAL': Decimal('5.0'),  # 5% for individuals
            'COMPANY': Decimal('3.0'),     # 3% for companies
            'RESIDENT': Decimal('15.0'),   # 15% for non-residents
        }
        
        rate = wht_rates.get(customer_type, Decimal('5.0'))
        wht_amount = (Decimal(str(amount)) * rate) / 100
        
        return {
            'wht_rate': rate,
            'wht_amount': wht_amount.quantize(Decimal('0.01')),
            'net_amount': (Decimal(str(amount)) - wht_amount).quantize(Decimal('0.01'))
        }

    @staticmethod
    def calculate_excise_duty(amount, service_type='INTERNET'):
        """Calculate Excise Duty for Kenya"""
        # Excise Duty rates in Kenya (as of 2024)
        excise_rates = {
            'INTERNET': Decimal('15.0'),      # 15% for internet services
            'VOIP': Decimal('20.0'),          # 20% for VoIP services
            'SMS': Decimal('12.0'),           # 12% for SMS services
            'VOICE': Decimal('12.0'),         # 12% for voice calls
            'DATA': Decimal('15.0'),          # 15% for data services
        }
        
        rate = excise_rates.get(service_type, Decimal('15.0'))
        excise_amount = (Decimal(str(amount)) * rate) / 100
        
        return {
            'excise_rate': rate,
            'excise_amount': excise_amount.quantize(Decimal('0.01')),
            'total_with_excise': (Decimal(str(amount)) + excise_amount).quantize(Decimal('0.01'))
        }

    @staticmethod
    def calculate_total_taxes(amount, customer_type='INDIVIDUAL', service_type='INTERNET', 
                              include_vat=True, include_wht=False, include_excise=True):
        """Calculate all applicable taxes"""
        result = {
            'base_amount': Decimal(str(amount)),
            'vat_amount': Decimal('0'),
            'wht_amount': Decimal('0'),
            'excise_amount': Decimal('0'),
            'total_tax': Decimal('0'),
            'total_amount': Decimal(str(amount))
        }
        
        # Calculate VAT if included
        if include_vat:
            vat_calc = TaxCalculator.calculate_vat(amount, is_inclusive=False)
            result['vat_amount'] = vat_calc['vat_amount']
            result['total_amount'] = vat_calc['total_amount']
        
        # Calculate Excise Duty if included
        if include_excise:
            excise_calc = TaxCalculator.calculate_excise_duty(result['total_amount'], service_type)
            result['excise_amount'] = excise_calc['excise_amount']
            result['total_amount'] = excise_calc['total_with_excise']
        
        # Calculate Withholding Tax if included
        if include_wht:
            wht_calc = TaxCalculator.calculate_withholding_tax(result['total_amount'], customer_type)
            result['wht_amount'] = wht_calc['wht_amount']
            result['total_amount'] = wht_calc['net_amount']
        
        # Calculate total tax
        result['total_tax'] = (
            result['vat_amount'] + 
            result['wht_amount'] + 
            result['excise_amount']
        )
        
        # Round all amounts
        for key in result:
            if isinstance(result[key], Decimal):
                result[key] = result[key].quantize(Decimal('0.01'))
        
        return result

    @staticmethod
    def generate_tax_invoice(invoice):
        """Generate tax breakdown for an invoice"""
        customer = invoice.customer
        service_type = invoice.service_connection.service_type if invoice.service_connection else 'INTERNET'
        
        # Determine customer type for WHT
        if customer.customer_type in ['BUSINESS', 'CORPORATE', 'INSTITUTION']:
            customer_type = 'COMPANY'
        else:
            customer_type = 'INDIVIDUAL'
        
        # Calculate taxes
        tax_breakdown = TaxCalculator.calculate_total_taxes(
            amount=invoice.subtotal,
            customer_type=customer_type,
            service_type=service_type,
            include_vat=True,
            include_wht=customer.customer_type in ['CORPORATE', 'GOVERNMENT'],
            include_excise=True
        )
        
        return {
            'invoice_number': invoice.invoice_number,
            'customer': {
                'name': customer.full_name,
                'type': customer.customer_type,
                'tax_pin': customer.tax_pin if hasattr(customer, 'tax_pin') else None
            },
            'tax_breakdown': tax_breakdown,
            'items': [
                {
                    'description': item.description,
                    'amount': item.total,
                    'tax_amount': item.tax_amount
                }
                for item in invoice.items.all()
            ]
        }

    @staticmethod
    def is_tax_exempt(customer, service_type):
        """Check if customer is tax exempt for a particular service"""
        # Government and diplomatic missions are often tax exempt
        if customer.customer_type == 'GOVERNMENT':
            return True
        
        # Check for tax exemption certificate
        if hasattr(customer, 'tax_exempt') and customer.tax_exempt:
            return True
        
        # Specific service exemptions
        if service_type in ['EDUCATION', 'HEALTH'] and customer.customer_type in ['INSTITUTION', 'NGO']:
            return True
        
        return False
