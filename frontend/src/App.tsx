import { DashboardPage } from './features/dashboard/DashboardPage'
import { ThemeProvider } from './theme/ThemeProvider'

function App(): React.JSX.Element {
  return (
    <ThemeProvider>
      <DashboardPage />
    </ThemeProvider>
  )
}

export default App
