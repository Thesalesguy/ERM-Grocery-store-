import { Route, Routes } from 'react-router-dom'
import { AppShell } from './components/layout/AppShell'
import { ProtectedRoute, RequirePermission } from './auth/ProtectedRoute'
import { AccountingPage } from './pages/AccountingPage'
import { AccountsPayablePage } from './pages/AccountsPayablePage'
import { DashboardPage } from './pages/DashboardPage'
import { HrPage } from './pages/HrPage'
import { InventoryPage } from './pages/InventoryPage'
import { LoginPage } from './pages/LoginPage'
import { PayrollPage } from './pages/PayrollPage'
import { PosPage } from './pages/PosPage'
import { ProductsPage } from './pages/ProductsPage'
import { PurchasingPage } from './pages/PurchasingPage'
import { ReportsPage } from './pages/ReportsPage'
import { SalesPage } from './pages/SalesPage'
import { SettingsPage } from './pages/SettingsPage'
import { StockCountsPage } from './pages/StockCountsPage'
import { SupplyChainPage } from './pages/SupplyChainPage'
import { SuppliersPage } from './pages/SuppliersPage'
import { TransfersPage } from './pages/TransfersPage'
import { UsersPage } from './pages/UsersPage'

function App() {
  return (
    <Routes>
      <Route path="login" element={<LoginPage />} />

      <Route element={<ProtectedRoute />}>
        <Route element={<AppShell />}>
          <Route index element={<DashboardPage />} />
          <Route
            path="pos"
            element={
              <RequirePermission permission="pos.use">
                <PosPage />
              </RequirePermission>
            }
          />
          <Route
            path="products"
            element={
              <RequirePermission permission="products.read">
                <ProductsPage />
              </RequirePermission>
            }
          />
          <Route
            path="inventory"
            element={
              <RequirePermission permission="inventory.read">
                <InventoryPage />
              </RequirePermission>
            }
          />
          <Route
            path="stock-counts"
            element={
              <RequirePermission permission="inventory.read">
                <StockCountsPage />
              </RequirePermission>
            }
          />
          <Route
            path="transfers"
            element={
              <RequirePermission permission="inventory.read">
                <TransfersPage />
              </RequirePermission>
            }
          />
          <Route
            path="purchasing"
            element={
              <RequirePermission permission="purchasing.read">
                <PurchasingPage />
              </RequirePermission>
            }
          />
          <Route
            path="suppliers"
            element={
              <RequirePermission permission="purchasing.read">
                <SuppliersPage />
              </RequirePermission>
            }
          />
          <Route
            path="supply-chain"
            element={
              <RequirePermission permission="supply_chain.read">
                <SupplyChainPage />
              </RequirePermission>
            }
          />
          <Route
            path="sales"
            element={
              <RequirePermission permission="sales.read">
                <SalesPage />
              </RequirePermission>
            }
          />
          <Route
            path="accounts-payable"
            element={
              <RequirePermission permission="ap.read">
                <AccountsPayablePage />
              </RequirePermission>
            }
          />
          <Route
            path="accounting"
            element={
              <RequirePermission permission="accounting.read">
                <AccountingPage />
              </RequirePermission>
            }
          />
          <Route
            path="reports"
            element={
              <RequirePermission permission="reports.read">
                <ReportsPage />
              </RequirePermission>
            }
          />
          <Route
            path="users"
            element={
              <RequirePermission permission="users.manage">
                <UsersPage />
              </RequirePermission>
            }
          />
          <Route
            path="hr"
            element={
              <RequirePermission permission="hr.read">
                <HrPage />
              </RequirePermission>
            }
          />
          <Route
            path="payroll"
            element={
              <RequirePermission permission="payroll.read">
                <PayrollPage />
              </RequirePermission>
            }
          />
          <Route
            path="settings"
            element={
              <RequirePermission permission="store.settings.read">
                <SettingsPage />
              </RequirePermission>
            }
          />
        </Route>
      </Route>
    </Routes>
  )
}

export default App
