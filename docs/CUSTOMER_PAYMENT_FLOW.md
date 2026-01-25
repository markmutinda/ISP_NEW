# Customer Payment Flow & Self-Registration Implementation Plan

## Overview

This document outlines the complete payment flow for end-users (WiFi/Hotspot customers) paying for internet services, including:

1. **Payment Split Logic**: 5% to Netily, 95% to ISP Provider
2. **Customer Self-Registration**: Allow end-users to register on ISP subdomains
3. **Frontend Recommendations**: Alignment guide for frontend developers

---

## Table of Contents

1. [Payment Architecture](#1-payment-architecture)
2. [Payment Split Implementation](#2-payment-split-implementation)
3. [Customer Self-Registration](#3-customer-self-registration)
4. [API Endpoints Reference](#4-api-endpoints-reference)
5. [Frontend Recommendations](#5-frontend-recommendations)
6. [Implementation TODO List](#6-implementation-todo-list)
7. [Database Schema Updates](#7-database-schema-updates)

---

## 1. Payment Architecture

### Current Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          PAYMENT ARCHITECTURE                                │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  1. ISP SUBSCRIPTION PAYMENTS (ISP → Netily)                                │
│     ─────────────────────────────────────────                               │
│     ISP pays Netily for platform subscription (Starter/Pro/Enterprise)     │
│     100% goes to Netily - no split needed                                   │
│                                                                              │
│  2. HOTSPOT/WIFI PAYMENTS (End User → Netily → ISP)                        │
│     ────────────────────────────────────────────                            │
│     End user pays for WiFi access at hotspot                                │
│     Payment goes to Netily's PayHero account                                │
│     Netily takes 5% commission, settles 95% to ISP                         │
│                                                                              │
│  3. CUSTOMER BILLING PAYMENTS (Customer → Netily → ISP)                     │
│     ────────────────────────────────────────────────                        │
│     ISP customer pays monthly invoice/recharge                              │
│     Payment goes to Netily's PayHero account                                │
│     Netily takes 5% commission, settles 95% to ISP                         │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Payment Flow Diagram

```
┌───────────────┐     ┌───────────────┐     ┌───────────────┐     ┌───────────────┐
│   End User    │     │   PayHero     │     │    Netily     │     │     ISP       │
│   (Customer)  │     │   (Gateway)   │     │   (Platform)  │     │   (Provider)  │
└───────┬───────┘     └───────┬───────┘     └───────┬───────┘     └───────┬───────┘
        │                     │                     │                     │
        │ 1. Initiate Payment │                     │                     │
        │─────────────────────>                     │                     │
        │                     │                     │                     │
        │ 2. STK Push to Phone│                     │                     │
        │<─────────────────────                     │                     │
        │                     │                     │                     │
        │ 3. Enter M-Pesa PIN │                     │                     │
        │─────────────────────>                     │                     │
        │                     │                     │                     │
        │                     │ 4. Payment Callback │                     │
        │                     │─────────────────────>                     │
        │                     │                     │                     │
        │                     │                     │ 5. Record Commission│
        │                     │                     │    (5% to Netily)   │
        │                     │                     │                     │
        │                     │                     │ 6. Update ISP Balance│
        │                     │                     │    (95% pending)    │
        │                     │                     │─────────────────────>
        │                     │                     │                     │
        │                     │                     │ 7. Periodic Settlement│
        │                     │                     │    (B2C to ISP)     │
        │                     │                     │─────────────────────>
        │                     │                     │                     │
```

---

## 2. Payment Split Implementation

### 2.1 Current Implementation (Already Done ✅)

The payment split logic is **already implemented** in the backend:

**File**: [apps/subscriptions/models.py](../apps/subscriptions/models.py)

```python
# CommissionLedger.record_commission() method
# Default: NETILY_COMMISSION_RATE = 0.05 (5%)

rate = commission_rate or Decimal(str(getattr(settings, 'NETILY_COMMISSION_RATE', 0.05)))
gross = Decimal(str(gross_amount))
commission = (gross * rate).quantize(Decimal('0.01'))  # 5% to Netily
isp_amount = gross - commission                         # 95% to ISP
```

### 2.2 Models Involved

| Model | Schema | Purpose |
|-------|--------|---------|
| `CommissionLedger` | public | Tracks Netily's 5% on each payment |
| `ISPPayoutConfig` | public | ISP's M-Pesa/Bank details for settlement |
| `ISPSettlement` | public | Records of payouts to ISPs |
| `HotspotSession` | tenant | Hotspot WiFi payment records |
| `Payment` | tenant | Customer billing payment records |

### 2.3 Payment Split Flow

When a payment is received via webhook:

```python
# In webhook_views.py - PayHeroHotspotWebhookView

# After payment success:
CommissionLedger.record_commission(
    company=company,
    payment_type='hotspot',  # or 'recharge', 'invoice'
    payment_reference=session.session_id,
    gross_amount=session.amount,
    # commission_rate defaults to 0.05 (5%)
)
```

This automatically:
1. Records 5% commission to Netily in `CommissionLedger`
2. Updates ISP's pending balance in `ISPPayoutConfig`
3. The pending balance is settled periodically via B2C

### 2.4 ISP Payout Configuration

ISPs must configure their payout details in their dashboard:

**Endpoint**: `POST /api/v1/subscriptions/payout-config/`

```json
{
    "payout_method": "mpesa_b2c",  // or "bank_transfer"
    "mpesa_phone": "254712345678",
    "mpesa_name": "John Doe",
    "settlement_frequency": "weekly",  // daily, weekly, biweekly, monthly
    "minimum_payout": 1000.00
}
```

### 2.5 Configuration Variable

Add to `.env`:

```env
# Netily Commission Rate (5% = 0.05)
NETILY_COMMISSION_RATE=0.05
```

And in `config/settings/base.py`:

```python
NETILY_COMMISSION_RATE = float(os.getenv('NETILY_COMMISSION_RATE', 0.05))
```

---

## 3. Customer Self-Registration

### 3.1 Problem Statement

Currently, customers (end-users who want to pay for WiFi/internet) cannot self-register on an ISP's subdomain. They must be created by ISP staff.

### 3.2 Solution: Customer Self-Service Registration

Create a **PUBLIC** registration endpoint that:
1. Works on tenant subdomains (e.g., `yellow.localhost`)
2. Creates a User in the tenant schema
3. Creates a Customer profile
4. Optionally sends verification email/SMS
5. Issues JWT tokens for immediate access

### 3.3 New Endpoint

**`POST /api/v1/self-service/register/`**

```json
// Request
{
    "email": "customer@example.com",
    "phone_number": "254712345678",
    "first_name": "John",
    "last_name": "Doe",
    "password": "SecurePass123!",
    "id_number": "12345678"  // Optional
}

// Response
{
    "status": "success",
    "user": {
        "id": 1,
        "email": "customer@example.com",
        "first_name": "John",
        "last_name": "Doe"
    },
    "customer": {
        "id": 1,
        "customer_code": "CUST-0001",
        "status": "PENDING"  // or "ACTIVE" based on ISP settings
    },
    "access": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
    "refresh": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
    "message": "Registration successful. Please verify your phone number."
}
```

### 3.4 Implementation Details

See [Implementation TODO List](#6-implementation-todo-list) for step-by-step tasks.

---

## 4. API Endpoints Reference

### 4.1 Customer Self-Service Endpoints (Public on Tenant Subdomain)

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `POST` | `/api/v1/self-service/register/` | ❌ | Customer self-registration |
| `POST` | `/api/v1/self-service/login/` | ❌ | Customer login (phone/email + password) |
| `POST` | `/api/v1/self-service/verify-phone/` | ❌ | Verify phone via OTP |
| `POST` | `/api/v1/self-service/resend-otp/` | ❌ | Resend verification OTP |
| `GET` | `/api/v1/self-service/plans/` | ❌ | List available ISP plans |

### 4.2 Customer Dashboard Endpoints (Authenticated)

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `GET` | `/api/v1/self-service/dashboard/` | ✅ | Customer dashboard data |
| `GET` | `/api/v1/self-service/payments/` | ✅ | Payment history |
| `POST` | `/api/v1/self-service/payments/` | ✅ | Initiate M-Pesa payment |
| `GET` | `/api/v1/self-service/payments/{id}/status/` | ✅ | Check payment status |
| `GET` | `/api/v1/self-service/service-requests/` | ✅ | List service requests |
| `POST` | `/api/v1/self-service/service-requests/` | ✅ | Create service request |
| `GET` | `/api/v1/self-service/alerts/` | ✅ | List alerts |

### 4.3 Hotspot Endpoints (Public - No Auth)

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `GET` | `/api/v1/hotspot/routers/{id}/plans/` | ❌ | Get hotspot plans |
| `POST` | `/api/v1/hotspot/purchase/` | ❌ | Initiate hotspot purchase |
| `GET` | `/api/v1/hotspot/purchase/{session_id}/status/` | ❌ | Check purchase status |

### 4.4 ISP Admin Endpoints (Staff Only)

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `GET` | `/api/v1/subscriptions/payout-config/` | ✅ | Get payout config |
| `POST` | `/api/v1/subscriptions/payout-config/` | ✅ | Set payout config |
| `POST` | `/api/v1/subscriptions/payout-config/verify/` | ✅ | Verify payout details |
| `GET` | `/api/v1/subscriptions/settlements/` | ✅ | List settlements |

---

## 5. Frontend Recommendations

### 5.1 API Configuration

**CRITICAL**: Use subdomain-aware API URL construction:

```typescript
// ❌ WRONG - Loses subdomain context
const API_URL = 'http://127.0.0.1:8000/api/v1';

// ✅ CORRECT - Preserves subdomain
const getApiUrl = () => {
  const hostname = window.location.hostname;
  const port = process.env.NODE_ENV === 'development' ? ':8000' : '';
  const protocol = window.location.protocol;
  return `${protocol}//${hostname}${port}/api/v1`;
};

// Usage
const API_URL = getApiUrl();
// On yellow.localhost:3000 → http://yellow.localhost:8000/api/v1
```

### 5.2 Project Structure

```
frontend/
├── src/
│   ├── api/
│   │   ├── config.ts           # API configuration
│   │   ├── auth.ts             # Auth endpoints
│   │   ├── billing.ts          # Billing/payment endpoints
│   │   ├── customers.ts        # Customer endpoints
│   │   └── hotspot.ts          # Hotspot endpoints
│   │
│   ├── components/
│   │   ├── auth/
│   │   │   ├── LoginForm.tsx
│   │   │   ├── RegisterForm.tsx      # Customer self-registration
│   │   │   └── PhoneVerification.tsx
│   │   │
│   │   ├── billing/
│   │   │   ├── PaymentForm.tsx
│   │   │   ├── PaymentStatus.tsx
│   │   │   ├── InvoiceList.tsx
│   │   │   └── MpesaPaymentModal.tsx
│   │   │
│   │   ├── hotspot/
│   │   │   ├── CaptivePortal.tsx     # Hotspot landing page
│   │   │   ├── PlanSelector.tsx
│   │   │   └── PurchaseStatus.tsx
│   │   │
│   │   └── dashboard/
│   │       ├── CustomerDashboard.tsx
│   │       ├── UsageStats.tsx
│   │       └── AccountBalance.tsx
│   │
│   ├── hooks/
│   │   ├── useAuth.ts
│   │   ├── usePayment.ts
│   │   └── useTenant.ts              # Get current tenant context
│   │
│   ├── contexts/
│   │   ├── AuthContext.tsx
│   │   └── TenantContext.tsx
│   │
│   ├── pages/
│   │   ├── public/
│   │   │   ├── Login.tsx
│   │   │   ├── Register.tsx          # Customer registration
│   │   │   └── HotspotPortal.tsx
│   │   │
│   │   ├── customer/
│   │   │   ├── Dashboard.tsx
│   │   │   ├── Payments.tsx
│   │   │   └── Profile.tsx
│   │   │
│   │   └── admin/                    # ISP staff pages
│   │       ├── Dashboard.tsx
│   │       ├── Customers.tsx
│   │       ├── PayoutSettings.tsx
│   │       └── Settlements.tsx
│   │
│   └── utils/
│       ├── api.ts                    # Axios instance with subdomain
│       └── formatters.ts
```

### 5.3 Key Components

#### 5.3.1 Customer Registration Form

```typescript
// components/auth/CustomerRegisterForm.tsx

interface RegisterFormData {
  email: string;
  phone_number: string;
  first_name: string;
  last_name: string;
  password: string;
  password_confirm: string;
  id_number?: string;
}

const CustomerRegisterForm: React.FC = () => {
  const [formData, setFormData] = useState<RegisterFormData>({...});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  
  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setLoading(true);
    
    try {
      const response = await api.post('/self-service/register/', formData);
      
      // Store tokens
      localStorage.setItem('access_token', response.data.access);
      localStorage.setItem('refresh_token', response.data.refresh);
      
      // Redirect to dashboard or verification
      if (response.data.requires_verification) {
        navigate('/verify-phone');
      } else {
        navigate('/dashboard');
      }
    } catch (err) {
      setError(err.response?.data?.error || 'Registration failed');
    } finally {
      setLoading(false);
    }
  };
  
  return (
    <form onSubmit={handleSubmit}>
      {/* Form fields */}
    </form>
  );
};
```

#### 5.3.2 Payment Component

```typescript
// components/billing/MpesaPaymentModal.tsx

interface PaymentModalProps {
  amount: number;
  invoiceId?: number;
  onSuccess: (payment: Payment) => void;
  onClose: () => void;
}

const MpesaPaymentModal: React.FC<PaymentModalProps> = ({
  amount,
  invoiceId,
  onSuccess,
  onClose
}) => {
  const [phoneNumber, setPhoneNumber] = useState('');
  const [status, setStatus] = useState<'idle' | 'pending' | 'success' | 'failed'>('idle');
  const [paymentId, setPaymentId] = useState<number | null>(null);
  
  const initiatePayment = async () => {
    setStatus('pending');
    
    try {
      const response = await api.post('/billing/payments/initiate/', {
        amount,
        phone_number: phoneNumber,
        invoice_id: invoiceId
      });
      
      setPaymentId(response.data.payment_id);
      
      // Start polling for status
      pollPaymentStatus(response.data.payment_id);
    } catch (err) {
      setStatus('failed');
    }
  };
  
  const pollPaymentStatus = async (id: number) => {
    const interval = setInterval(async () => {
      const response = await api.get(`/billing/payments/${id}/status/`);
      
      if (response.data.status === 'completed') {
        clearInterval(interval);
        setStatus('success');
        onSuccess(response.data);
      } else if (response.data.status === 'failed') {
        clearInterval(interval);
        setStatus('failed');
      }
    }, 3000); // Poll every 3 seconds
    
    // Stop after 2 minutes
    setTimeout(() => clearInterval(interval), 120000);
  };
  
  return (
    <Modal onClose={onClose}>
      {status === 'idle' && (
        <>
          <h2>Pay with M-Pesa</h2>
          <p>Amount: KES {amount.toLocaleString()}</p>
          <input
            type="tel"
            placeholder="254712345678"
            value={phoneNumber}
            onChange={(e) => setPhoneNumber(e.target.value)}
          />
          <button onClick={initiatePayment}>
            Send STK Push
          </button>
        </>
      )}
      
      {status === 'pending' && (
        <div>
          <Spinner />
          <p>Check your phone and enter M-Pesa PIN...</p>
        </div>
      )}
      
      {status === 'success' && (
        <div>
          <CheckIcon />
          <p>Payment successful!</p>
        </div>
      )}
      
      {status === 'failed' && (
        <div>
          <XIcon />
          <p>Payment failed. Please try again.</p>
          <button onClick={() => setStatus('idle')}>Retry</button>
        </div>
      )}
    </Modal>
  );
};
```

#### 5.3.3 Hotspot Captive Portal

```typescript
// pages/public/HotspotPortal.tsx

const HotspotPortal: React.FC = () => {
  const { routerId } = useParams();
  const [plans, setPlans] = useState<HotspotPlan[]>([]);
  const [branding, setBranding] = useState<Branding | null>(null);
  const [selectedPlan, setSelectedPlan] = useState<HotspotPlan | null>(null);
  const [phoneNumber, setPhoneNumber] = useState('');
  const [macAddress, setMacAddress] = useState('');
  const [status, setStatus] = useState<'selecting' | 'paying' | 'connected'>('selecting');
  
  useEffect(() => {
    // Fetch plans and branding
    fetchPlans();
    
    // Try to get MAC address (may not work in all browsers)
    // Usually passed as URL parameter from captive portal redirect
    const params = new URLSearchParams(window.location.search);
    setMacAddress(params.get('mac') || '');
  }, [routerId]);
  
  const fetchPlans = async () => {
    const response = await api.get(`/hotspot/routers/${routerId}/plans/`);
    setPlans(response.data.plans);
    setBranding(response.data.branding);
  };
  
  const handlePurchase = async () => {
    if (!selectedPlan) return;
    
    setStatus('paying');
    
    const response = await api.post('/hotspot/purchase/', {
      router_id: routerId,
      plan_id: selectedPlan.id,
      phone_number: phoneNumber,
      mac_address: macAddress
    });
    
    // Poll for status
    pollStatus(response.data.session_id);
  };
  
  return (
    <div style={{ background: branding?.primary_color }}>
      {branding?.logo_url && <img src={branding.logo_url} alt="Logo" />}
      <h1>{branding?.welcome_title || 'Connect to WiFi'}</h1>
      
      {status === 'selecting' && (
        <>
          <div className="plans-grid">
            {plans.map(plan => (
              <PlanCard
                key={plan.id}
                plan={plan}
                selected={selectedPlan?.id === plan.id}
                onSelect={() => setSelectedPlan(plan)}
              />
            ))}
          </div>
          
          {selectedPlan && (
            <div>
              <input
                type="tel"
                placeholder="M-Pesa Number (254...)"
                value={phoneNumber}
                onChange={(e) => setPhoneNumber(e.target.value)}
              />
              <button onClick={handlePurchase}>
                Pay KES {selectedPlan.price}
              </button>
            </div>
          )}
        </>
      )}
      
      {status === 'paying' && <PaymentStatus />}
      {status === 'connected' && <ConnectionSuccess />}
    </div>
  );
};
```

### 5.4 Axios Configuration

```typescript
// utils/api.ts

import axios from 'axios';

const getBaseURL = () => {
  const hostname = window.location.hostname;
  const isDev = process.env.NODE_ENV === 'development';
  const port = isDev ? ':8000' : '';
  const protocol = window.location.protocol;
  
  return `${protocol}//${hostname}${port}/api/v1`;
};

const api = axios.create({
  baseURL: getBaseURL(),
  headers: {
    'Content-Type': 'application/json',
  },
});

// Add auth token to requests
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Handle token refresh
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config;
    
    if (error.response?.status === 401 && !originalRequest._retry) {
      originalRequest._retry = true;
      
      try {
        const refreshToken = localStorage.getItem('refresh_token');
        const response = await axios.post(`${getBaseURL()}/auth/token/refresh/`, {
          refresh: refreshToken,
        });
        
        localStorage.setItem('access_token', response.data.access);
        originalRequest.headers.Authorization = `Bearer ${response.data.access}`;
        
        return api(originalRequest);
      } catch (refreshError) {
        // Redirect to login
        localStorage.removeItem('access_token');
        localStorage.removeItem('refresh_token');
        window.location.href = '/login';
      }
    }
    
    return Promise.reject(error);
  }
);

export default api;
```

### 5.5 Environment Variables

```env
# .env.development
REACT_APP_API_PORT=8000

# .env.production
# No port needed - same domain as frontend
```

### 5.6 Routing Configuration

```typescript
// App.tsx or routes.tsx

const routes = [
  // Public routes (no auth required)
  { path: '/login', element: <Login />, public: true },
  { path: '/register', element: <CustomerRegister />, public: true },
  { path: '/verify-phone', element: <PhoneVerification />, public: true },
  { path: '/hotspot/:routerId', element: <HotspotPortal />, public: true },
  
  // Customer routes (customer auth required)
  { path: '/dashboard', element: <CustomerDashboard />, role: 'customer' },
  { path: '/payments', element: <Payments />, role: 'customer' },
  { path: '/invoices', element: <Invoices />, role: 'customer' },
  { path: '/profile', element: <Profile />, role: 'customer' },
  
  // ISP Admin routes (staff auth required)
  { path: '/admin/dashboard', element: <AdminDashboard />, role: 'admin' },
  { path: '/admin/customers', element: <CustomersManagement />, role: 'admin' },
  { path: '/admin/billing', element: <BillingManagement />, role: 'admin' },
  { path: '/admin/settings/payout', element: <PayoutSettings />, role: 'admin' },
  { path: '/admin/settlements', element: <Settlements />, role: 'admin' },
];
```

---

## 6. Implementation TODO List

### Phase 1: Customer Self-Registration (Priority: HIGH)

- [ ] **1.1** Create `CustomerSelfRegisterView` in `apps/self_service/views.py`
- [ ] **1.2** Create `CustomerSelfRegisterSerializer` for validation
- [ ] **1.3** Add phone/email verification via OTP
- [ ] **1.4** Update `apps/self_service/urls.py` with new endpoints
- [ ] **1.5** Create ISP settings for registration (auto-approve, require verification, etc.)
- [ ] **1.6** Add tests for registration flow

### Phase 2: Payment Flow Enhancements (Priority: HIGH)

- [ ] **2.1** Add `NETILY_COMMISSION_RATE` to settings
- [ ] **2.2** Verify `CommissionLedger` records correctly on all payment types
- [ ] **2.3** Implement settlement job (Celery task) for B2C payouts
- [ ] **2.4** Add settlement notifications (email/SMS to ISP)
- [ ] **2.5** Create settlement report endpoints

### Phase 3: ISP Payout Configuration (Priority: MEDIUM)

- [ ] **3.1** Create UI for ISP to configure payout details
- [ ] **3.2** Implement M-Pesa name verification (small test payment)
- [ ] **3.3** Add bank account verification
- [ ] **3.4** Create payout history/statements

### Phase 4: Hotspot Enhancements (Priority: MEDIUM)

- [ ] **4.1** Add ISP branding configuration UI
- [ ] **4.2** Implement MikroTik API integration for user creation
- [ ] **4.3** Add data usage tracking
- [ ] **4.4** Session expiry notifications

### Phase 5: Frontend Development (Priority: HIGH)

- [ ] **5.1** Set up project with subdomain-aware API
- [ ] **5.2** Create customer registration form
- [ ] **5.3** Create payment components
- [ ] **5.4** Create customer dashboard
- [ ] **5.5** Create hotspot captive portal
- [ ] **5.6** Create ISP admin payout settings page

---

## 7. Database Schema Updates

### 7.1 New Settings for Self-Registration

Add to `ISP Settings` (or create new model):

```python
# apps/customers/models.py

class ISPRegistrationSettings(models.Model):
    """Settings for customer self-registration per ISP"""
    
    # Auto-created per tenant
    
    allow_self_registration = models.BooleanField(
        default=True,
        help_text="Allow customers to register themselves"
    )
    require_phone_verification = models.BooleanField(
        default=True,
        help_text="Require phone OTP verification"
    )
    require_email_verification = models.BooleanField(
        default=False,
        help_text="Require email verification"
    )
    require_id_number = models.BooleanField(
        default=False,
        help_text="Require national ID number"
    )
    auto_approve_customers = models.BooleanField(
        default=False,
        help_text="Auto-approve new registrations (vs manual approval)"
    )
    default_customer_status = models.CharField(
        max_length=20,
        choices=CUSTOMER_STATUS_CHOICES,
        default='PENDING'
    )
    welcome_sms_template = models.TextField(
        blank=True,
        help_text="SMS sent after registration. Use {name}, {customer_code}"
    )
```

### 7.2 Phone Verification Model

```python
# apps/core/models.py

class PhoneVerification(models.Model):
    """OTP verification for phone numbers"""
    
    phone_number = models.CharField(max_length=15)
    otp_code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    verified = models.BooleanField(default=False)
    attempts = models.PositiveIntegerField(default=0)
    
    class Meta:
        indexes = [
            models.Index(fields=['phone_number', 'otp_code']),
        ]
```

---

## Summary

### What's Already Implemented ✅

1. **Payment split logic** (5% Netily, 95% ISP) via `CommissionLedger`
2. **ISP payout configuration** model (`ISPPayoutConfig`)
3. **Settlement tracking** (`ISPSettlement`)
4. **Hotspot payment flow** (public, no auth)
5. **Customer billing payment** webhooks

### What Needs Implementation 🚧

1. **Customer self-registration** endpoint
2. **Phone verification** (OTP)
3. **Settlement B2C job** (actual payout to ISP)
4. **Frontend components** (registration, payments, hotspot portal)
5. **MikroTik API integration** for hotspot user activation

### Key Configuration Points

| Setting | Value | Location |
|---------|-------|----------|
| Commission Rate | 5% (0.05) | `NETILY_COMMISSION_RATE` in `.env` |
| PayHero Callbacks | See below | `.env` |

```env
PAYHERO_SUBSCRIPTION_CALLBACK=https://api.netily.io/api/v1/webhooks/payhero/subscription/
PAYHERO_HOTSPOT_CALLBACK=https://api.netily.io/api/v1/webhooks/payhero/hotspot/
PAYHERO_BILLING_CALLBACK=https://api.netily.io/api/v1/webhooks/payhero/billing/
```
