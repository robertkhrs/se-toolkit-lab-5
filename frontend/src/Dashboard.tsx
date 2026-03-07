import { useState, useEffect } from 'react'
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  BarElement,
  LineElement,
  PointElement,
  Title,
  Tooltip,
  Legend,
} from 'chart.js'
import { Bar, Line } from 'react-chartjs-2'

// Register Chart.js components
ChartJS.register(
  CategoryScale,
  LinearScale,
  BarElement,
  LineElement,
  PointElement,
  Title,
  Tooltip,
  Legend,
)

// Types for API responses
interface ScoreBucket {
  bucket: string
  count: number
}

interface TimelineEntry {
  date: string
  submissions: number
}

interface PassRateEntry {
  task: string
  avg_score: number
  attempts: number
}

interface LabOption {
  id: string
  name: string
}

// Available labs for the dropdown
const LABS: LabOption[] = [
  { id: 'lab-01', name: 'Lab 01' },
  { id: 'lab-02', name: 'Lab 02' },
  { id: 'lab-03', name: 'Lab 03' },
  { id: 'lab-04', name: 'Lab 04' },
  { id: 'lab-05', name: 'Lab 05' },
]

const STORAGE_KEY = 'api_key'

function getApiKey(): string {
  return localStorage.getItem(STORAGE_KEY) ?? ''
}

async function fetchWithAuth<T>(url: string, params: Record<string, string>): Promise<T> {
  const token = getApiKey()
  const queryString = new URLSearchParams(params).toString()
  
  // ДОБАВЛЕНО: Базовый URL из переменной окружения
  const baseUrl = import.meta.env.VITE_API_TARGET || ''
  const fullUrl = `${baseUrl}${url}?${queryString}`

  const response = await fetch(fullUrl, {
    headers: {
      Authorization: `Bearer ${token}`,
    },
  })

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${response.statusText}`)
  }

  return response.json() as Promise<T>
}

export default function Dashboard() {
  const [selectedLab, setSelectedLab] = useState<string>('lab-04')
  const [scores, setScores] = useState<ScoreBucket[] | null>(null)
  const [timeline, setTimeline] = useState<TimelineEntry[] | null>(null)
  const [passRates, setPassRates] = useState<PassRateEntry[] | null>(null)
  const [loading, setLoading] = useState<boolean>(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const fetchData = async () => {
      setLoading(true)
      setError(null)

      try {
        const [scoresData, timelineData, passRatesData] = await Promise.all([
          fetchWithAuth<ScoreBucket[]>('/analytics/scores', { lab: selectedLab }),
          fetchWithAuth<TimelineEntry[]>('/analytics/timeline', { lab: selectedLab }),
          fetchWithAuth<PassRateEntry[]>('/analytics/pass-rates', { lab: selectedLab }),
        ])

        setScores(scoresData)
        setTimeline(timelineData)
        setPassRates(passRatesData)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unknown error')
      } finally {
        setLoading(false)
      }
    }

    fetchData()
  }, [selectedLab])

  // Bar chart data for score distribution
  const barChartData = {
    labels: scores?.map((s) => s.bucket) ?? [],
    datasets: [
      {
        label: 'Number of Students',
        data: scores?.map((s) => s.count) ?? [],
        backgroundColor: [
          'rgba(255, 99, 132, 0.6)',
          'rgba(255, 159, 64, 0.6)',
          'rgba(75, 192, 192, 0.6)',
          'rgba(54, 162, 235, 0.6)',
        ],
        borderColor: [
          'rgb(255, 99, 132)',
          'rgb(255, 159, 64)',
          'rgb(75, 192, 192)',
          'rgb(54, 162, 235)',
        ],
        borderWidth: 1,
      },
    ],
  }

  const barChartOptions = {
    responsive: true,
    plugins: {
      legend: {
        display: false,
      },
      title: {
        display: true,
        text: 'Score Distribution',
      },
    },
    scales: {
      y: {
        beginAtZero: true,
        ticks: {
          stepSize: 1,
        },
      },
    },
  }

  // Line chart data for timeline
  const lineChartData = {
    labels: timeline?.map((t) => t.date) ?? [],
    datasets: [
      {
        label: 'Submissions',
        data: timeline?.map((t) => t.submissions) ?? [],
        borderColor: 'rgb(75, 192, 192)',
        backgroundColor: 'rgba(75, 192, 192, 0.5)',
        tension: 0.1,
        fill: true,
      },
    ],
  }

  const lineChartOptions = {
    responsive: true,
    plugins: {
      legend: {
        display: false,
      },
      title: {
        display: true,
        text: 'Submissions Over Time',
      },
    },
    scales: {
      y: {
        beginAtZero: true,
        ticks: {
          stepSize: 1,
        },
      },
    },
  }

  return (
    <div className="dashboard">
      <h1>Analytics Dashboard</h1>

      {/* Lab selector */}
      <div className="lab-selector">
        <label htmlFor="lab-select">Select Lab: </label>
        <select
          id="lab-select"
          value={selectedLab}
          onChange={(e) => setSelectedLab(e.target.value)}
        >
          {LABS.map((lab) => (
            <option key={lab.id} value={lab.id}>
              {lab.name}
            </option>
          ))}
        </select>
      </div>

      {/* Loading and error states */}
      {loading && <p className="loading">Loading data...</p>}
      {error && <p className="error">Error: {error}</p>}

      {/* Charts and table */}
      {!loading && !error && (
        <div className="dashboard-content">
          <div className="chart-container">
            <Bar data={barChartData} options={barChartOptions} />
          </div>

          <div className="chart-container">
            <Line data={lineChartData} options={lineChartOptions} />
          </div>

          <div className="table-container">
            <h2>Pass Rates by Task</h2>
            {passRates && passRates.length > 0 ? (
              <table>
                <thead>
                  <tr>
                    <th>Task</th>
                    <th>Avg Score</th>
                    <th>Attempts</th>
                  </tr>
                </thead>
                <tbody>
                  {passRates.map((entry, index) => (
                    <tr key={index}>
                      <td>{entry.task}</td>
                      <td>{entry.avg_score.toFixed(1)}</td>
                      <td>{entry.attempts}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p>No pass rate data available.</p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}