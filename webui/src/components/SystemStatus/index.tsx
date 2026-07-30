// 侧边菜单底部系统状态：CPU/内存/硬盘/网络流量，每 5s 轮询 /v1/System/stats。
// 静默降级：接口失败或 host.available=false 时显示 "--"，不弹错误 toast。
import services from '@/services/luboman';
import { DashboardOutlined } from '@ant-design/icons';
import { Tooltip } from 'antd';
import React, { useEffect, useState } from 'react';
import styles from './index.less';

const POLL_INTERVAL = 5000;

function formatBytes(bytes?: number): string {
  if (bytes === undefined || bytes === null) return '--';
  if (bytes >= 1 << 30) return `${(bytes / (1 << 30)).toFixed(1)}G`;
  if (bytes >= 1 << 20) return `${(bytes / (1 << 20)).toFixed(1)}M`;
  if (bytes >= 1 << 10) return `${(bytes / (1 << 10)).toFixed(1)}K`;
  return `${bytes}B`;
}

function formatRate(bytesPerSec?: number): string {
  if (bytesPerSec === undefined || bytesPerSec === null) return '--';
  return `${formatBytes(Math.round(bytesPerSec))}/s`;
}

/** 按占用率取令牌色：>90% 红，>70% 黄，否则主色 */
function percentColor(percent?: number): string {
  if (percent === undefined || percent === null) return 'var(--lb-text-tertiary)';
  if (percent > 90) return 'var(--lb-error)';
  if (percent > 70) return 'var(--lb-warning)';
  return 'var(--lb-color-primary)';
}

const MeterRow: React.FC<{ label: string; percent?: number }> = ({ label, percent }) => (
  <div className={styles.row}>
    <span className={styles.label}>{label}</span>
    <span className={styles.track}>
      <span
        className={styles.bar}
        style={{
          width: `${Math.min(100, Math.max(0, percent ?? 0))}%`,
          background: percentColor(percent),
        }}
      />
    </span>
    <span className={styles.value}>
      {percent === undefined || percent === null ? '--' : `${percent.toFixed(0)}%`}
    </span>
  </div>
);

const SystemStatus: React.FC<{ collapsed?: boolean }> = ({ collapsed }) => {
  const [host, setHost] = useState<API.SystemHostStats | undefined>();

  useEffect(() => {
    let alive = true;
    const pull = async () => {
      try {
        const data = await services.System.getSystemStats({ skipErrorHandler: true });
        if (alive) setHost(data?.host);
      } catch {
        if (alive) setHost(undefined);
      }
    };
    pull();
    const timer = setInterval(pull, POLL_INTERVAL);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  const ok = host?.available;
  const cpu = ok ? host?.cpu_percent : undefined;
  const mem = ok ? host?.memory?.percent : undefined;
  const disk = ok ? host?.disk?.percent : undefined;
  const net = ok ? host?.network : undefined;

  const tooltipTitle = ok ? (
    <div>
      <div>CPU：{cpu?.toFixed(1)}%</div>
      <div>
        内存：{formatBytes(host?.memory?.used)} / {formatBytes(host?.memory?.total)}
      </div>
      <div>
        硬盘：剩余 {formatBytes(host?.disk?.free)} / {formatBytes(host?.disk?.total)}
      </div>
      <div>路径：{host?.disk?.path}</div>
      <div>
        网络：↑ {formatRate(net?.up_rate)}　↓ {formatRate(net?.down_rate)}
      </div>
    </div>
  ) : (
    '系统状态不可用'
  );

  if (collapsed) {
    return (
      <Tooltip title={tooltipTitle} placement="right">
        <div className={styles.collapsed}>
          <DashboardOutlined />
        </div>
      </Tooltip>
    );
  }

  return (
    <Tooltip title={tooltipTitle} placement="right">
      <div className={styles.container}>
        <MeterRow label="CPU" percent={cpu} />
        <MeterRow label="内存" percent={mem} />
        <MeterRow label="硬盘" percent={disk} />
        <div className={styles.row}>
          <span className={styles.label}>网络</span>
          <span className={styles.net}>
            ↑ {formatRate(net?.up_rate)}　↓ {formatRate(net?.down_rate)}
          </span>
        </div>
      </div>
    </Tooltip>
  );
};

export default SystemStatus;
