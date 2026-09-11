import { Route, Routes } from 'react-router-dom'
import { AppShell } from './components/layout/AppShell'
import { ProtectedRoute, RequirePermission } from './auth/ProtectedRoute'
import { DashboardPage } from './pages/DashboardPage'
import { InventoryPage } from './pages/InventoryPage'
import { LoginPage } from './pages/LoginPage'
import { PosPage } from './pages/PosPage'
import { ProductsPage } from './pages/ProductsPage'
import { PurchasingPage } from './pages/PurchasingPage'
import { ReportsPage } from './pages/ReportsPage'
import { SalesPage } from './pages/SalesPage'
import { SettingsPage } from './pages/SettingsPage'
import { SuppliersPage } from './pages/SuppliersPage'
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
            path="sales"
            element={
              <RequirePermission permission="sales.read">
                <SalesPage />
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
          <Route path="settings" element={<SettingsPage />} />
        </Route>
      </Route>
    </Routes>
  )
}

export default App
