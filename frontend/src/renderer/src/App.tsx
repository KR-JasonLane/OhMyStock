import { StatusPanel } from './components/StatusPanel'
import { useBackendStatus } from './hooks/useBackendStatus'

function App(): React.JSX.Element {
  const status = useBackendStatus()
  return (
    <main>
      <h1>OhMyStock</h1>
      <StatusPanel connected={status.connected} db={status.db} mode={status.mode} />
    </main>
  )
}

export default App
