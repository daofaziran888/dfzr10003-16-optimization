import { Link, Route, Routes } from 'react-router-dom'

import { ROLE_LABELS, SessionProvider, useSession, type OperatorRole } from './stores/session'
import Overview from './views/Overview'
import OdsView from './views/ods'
import DwdView from './views/dwd'
import DwsView from './views/dws'
import AdsView from './views/ads'
import DimdefView from './views/dimdef'
import FactdefView from './views/factdef'
import PartitionView from './views/partition'
import QualityView from './views/quality'
import LineageView from './views/lineage'
import BackfillView from './views/backfill'
import MetadataView from './views/metadata'
import EtlView from './views/etl'
import LakeView from './views/lake'
import CatalogView from './views/catalog'
import MonitorView from './views/monitor'
import SnapshotView from './views/snapshot'

const navItems = [{ label: "运营概览", path: "/" }, { label: "贴源层接入", path: "/ods" }, { label: "明细层清洗", path: "/dwd" }, { label: "汇总层聚合", path: "/dws" }, { label: "应用层输出", path: "/ads" }, { label: "维度定义管理", path: "/dimdef" }, { label: "事实表管理", path: "/factdef" }, { label: "分区策略", path: "/partition" }, { label: "质量探针", path: "/quality" }, { label: "血缘图谱", path: "/lineage" }, { label: "历史重跑", path: "/backfill" }, { label: "元数据目录", path: "/metadata" }, { label: "ETL任务调度", path: "/etl" }, { label: "数据湖存储", path: "/lake" }, { label: "表结构演进", path: "/catalog" }, { label: "运行监控面板", path: "/monitor" }, { label: "快照管理", path: "/snapshot" }]

function AppShell() {
  const { operator, canCleanup, switchRole } = useSession()

  return (
    <div className="app-shell">
      <aside className="app-side">
        <h1 className="app-title">企业数据仓库管理平台</h1>
        <nav className="nav-list">
          {navItems.map((item) => (
            <Link key={item.path} to={item.path} className="nav-item">
              {item.label}
            </Link>
          ))}
        </nav>
      </aside>
      <main className="app-main">
        <header className="app-head">
          <span className="head-desc">面向企业数据团队的一站式数据仓库建设与管理平台。覆盖原始数据落地、多层清洗转换、维度建模、事实建模、分区治理、质量校验、血缘追踪与历史补数的完整数仓链路。</span>
          <span className="head-user">
            当前值班：{operator.name}（{ROLE_LABELS[operator.role]}，{canCleanup ? '可清理' : '只读'}）
            <select
              className="role-select"
              value={operator.role}
              aria-label="切换值班身份"
              onChange={(event) => switchRole(event.target.value as OperatorRole)}
            >
              <option value="admin">{ROLE_LABELS.admin}</option>
              <option value="viewer">{ROLE_LABELS.viewer}</option>
            </select>
          </span>
        </header>
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/ods" element={<OdsView />} />
          <Route path="/dwd" element={<DwdView />} />
          <Route path="/dws" element={<DwsView />} />
          <Route path="/ads" element={<AdsView />} />
          <Route path="/dimdef" element={<DimdefView />} />
          <Route path="/factdef" element={<FactdefView />} />
          <Route path="/partition" element={<PartitionView />} />
          <Route path="/quality" element={<QualityView />} />
          <Route path="/lineage" element={<LineageView />} />
          <Route path="/backfill" element={<BackfillView />} />
          <Route path="/metadata" element={<MetadataView />} />
          <Route path="/etl" element={<EtlView />} />
          <Route path="/lake" element={<LakeView />} />
          <Route path="/catalog" element={<CatalogView />} />
          <Route path="/monitor" element={<MonitorView />} />
          <Route path="/snapshot" element={<SnapshotView />} />
        </Routes>
      </main>
    </div>
  )
}

export default function App() {
  return (
    <SessionProvider>
      <AppShell />
    </SessionProvider>
  )
}
