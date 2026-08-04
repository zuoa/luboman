import services from '@/services/luboman';
import { ReloadOutlined, ScissorOutlined } from '@ant-design/icons';
import { ActionType, ProColumns, ProTable } from '@ant-design/pro-components';
import {
  Button,
  Descriptions,
  Drawer,
  Empty,
  Popconfirm,
  Progress,
  Space,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import styles from './index.less';

const { listClipTask, getClipTask, getClipTaskStats, retryClipTask } =
  services.ClipTask;

const STATUS_VALUE_ENUM = {
  PENDING: { text: '排队中' },
  RUNNING: { text: '探测中' },
  SUCCESS: { text: '成功' },
  FAILED: { text: '失败' },
};

const STATUS_META: Record<string, { text: string; color: string }> = {
  PENDING: { text: '排队中', color: 'default' },
  RUNNING: { text: '探测中', color: 'processing' },
  SUCCESS: { text: '成功', color: 'success' },
  FAILED: { text: '失败', color: 'error' },
};

function formatDate(value?: string | null): string {
  if (!value) return '-';
  const time = new Date(value).getTime();
  return Number.isNaN(time) ? value : new Date(time).toLocaleString();
}

function formatJson(value: any): string {
  if (value == null) return '';
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function renderStatus(row: API.ClipTaskInfo) {
  const meta = STATUS_META[row.status || ''] || {
    text: row.status || '-',
    color: 'default',
  };
  return <Tag color={meta.color}>{meta.text}</Tag>;
}

function fileName(path?: string): string {
  if (!path) return '-';
  const parts = path.split('/');
  return parts[parts.length - 1] || path;
}

function formatSeconds(value?: number): string {
  if (value == null || Number.isNaN(value)) return '-';
  const seconds = Math.max(0, Math.floor(value));
  const min = Math.floor(seconds / 60);
  const sec = seconds % 60;
  return `${min.toString().padStart(2, '0')}:${sec.toString().padStart(2, '0')}`;
}

const ClipTaskList: React.FC = () => {
  const actionRef = useRef<ActionType>();
  const [stats, setStats] = useState<API.ClipTaskStats>();
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detail, setDetail] = useState<API.ClipTaskInfo>();

  const fetchStats = useCallback(async () => {
    try {
      setStats(await getClipTaskStats());
    } catch {
      setStats(undefined);
    }
  }, []);

  useEffect(() => {
    fetchStats();
  }, [fetchStats]);

  // 有排队/进行中的任务时轮询刷新列表与统计
  const hasActive = (stats?.active || 0) > 0;
  useEffect(() => {
    if (!hasActive) return;
    const timer = setInterval(() => {
      fetchStats();
      actionRef.current?.reload();
    }, 4000);
    return () => clearInterval(timer);
  }, [hasActive, fetchStats]);

  const reload = () => {
    fetchStats();
    actionRef.current?.reload();
  };

  const retryTask = async (row: API.ClipTaskInfo) => {
    try {
      await retryClipTask({ task_id: row.task_id });
      message.success('已重新排队执行');
      reload();
    } catch {
      // 失败提示由统一 errorHandler 弹出
    }
  };

  const openDetail = async (row: API.ClipTaskInfo) => {
    setDetailOpen(true);
    setDetail(row);
    setDetailLoading(true);
    try {
      setDetail(await getClipTask({ task_id: row.task_id }));
    } finally {
      setDetailLoading(false);
    }
  };

  const statItems = useMemo(
    () => [
      { label: '全部任务', value: stats?.total || 0 },
      { label: '进行中', value: stats?.active || 0 },
      { label: '成功', value: stats?.by_status?.SUCCESS || 0 },
      { label: '失败', value: stats?.by_status?.FAILED || 0 },
    ],
    [stats],
  );

  const columns: ProColumns<API.ClipTaskInfo>[] = [
    {
      title: '状态',
      dataIndex: 'status',
      valueType: 'select',
      valueEnum: STATUS_VALUE_ENUM,
      width: 110,
      render: (_, row) => renderStatus(row),
    },
    {
      title: '关键词',
      dataIndex: 'keyword',
      valueType: 'text',
      hideInTable: true,
      fieldProps: { placeholder: '任务 ID / 房间名' },
    },
    {
      title: '任务 ID',
      dataIndex: 'task_id',
      search: false,
      ellipsis: true,
      render: (_, row) => (
        <Typography.Text copyable className={styles.taskId}>
          {row.task_id}
        </Typography.Text>
      ),
    },
    {
      title: '房间',
      dataIndex: 'room_name',
      search: false,
      render: (_, row) =>
        row.room_name || <span className={styles.muted}>-</span>,
    },
    {
      title: '来源',
      dataIndex: 'source',
      width: 80,
      search: false,
      render: (_, row) =>
        row.source === 'AUTO' ? (
          <Tag color="geekblue">自动</Tag>
        ) : (
          <Tag>手动</Tag>
        ),
    },
    {
      title: '进度',
      dataIndex: 'progress',
      width: 150,
      search: false,
      render: (_, row) => (
        <Progress
          percent={row.progress || 0}
          size="small"
          status={
            row.status === 'FAILED'
              ? 'exception'
              : row.status === 'SUCCESS'
              ? 'success'
              : 'active'
          }
        />
      ),
    },
    {
      title: '来源文件数',
      dataIndex: 'record_file_count',
      width: 100,
      search: false,
      align: 'right',
      render: (_, row) => row.record_file_count || 0,
    },
    {
      title: '切片数',
      dataIndex: 'clip_count',
      width: 86,
      search: false,
      align: 'right',
      render: (_, row) => row.clip_count || 0,
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 170,
      search: false,
      render: (_, row) => formatDate(row.created_at),
    },
    {
      title: '结束时间',
      dataIndex: 'finished_at',
      width: 170,
      search: false,
      render: (_, row) => formatDate(row.finished_at),
    },
    {
      title: '错误',
      dataIndex: 'error_message',
      width: 180,
      search: false,
      ellipsis: true,
      render: (_, row) =>
        row.error_message ? (
          <Tooltip title={row.error_message}>
            <span className={styles.errorText}>{row.error_message}</span>
          </Tooltip>
        ) : (
          <span className={styles.muted}>-</span>
        ),
    },
    {
      title: '操作',
      valueType: 'option',
      width: 120,
      render: (_, row) => (
        <Space size={8}>
          <a onClick={() => openDetail(row)}>详情</a>
          {row.status === 'FAILED' && (
            <Popconfirm
              title="重新排队执行该任务？"
              okText="重试"
              cancelText="取消"
              onConfirm={() => retryTask(row)}
            >
              <a>重试</a>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  const intervalColumns = [
    {
      title: '来源录像',
      dataIndex: 'video',
      render: (value: string, row: API.ClipTaskFileIntervals) => (
        <Tooltip title={value}>
          <span className={styles.filePath}>
            {value ? fileName(value) : `录像 #${row.record_file_id}`}
          </span>
        </Tooltip>
      ),
    },
    {
      title: '探测区间',
      dataIndex: 'intervals',
      width: 320,
      render: (_: any, row: API.ClipTaskFileIntervals) => {
        if (row.error) {
          return <span className={styles.errorText}>{row.error}</span>;
        }
        if (!row.intervals?.length) {
          return <span className={styles.muted}>未检出三分屏片段</span>;
        }
        return (
          <Space size={4} wrap>
            {row.intervals.map(([start, end], index) => (
              <Tag key={index}>
                {formatSeconds(start)} - {formatSeconds(end)}
              </Tag>
            ))}
          </Space>
        );
      },
    },
  ];

  return (
    <div className={styles.pageShell}>
      <div className={styles.statsBar}>
        {statItems.map((item) => (
          <div className={styles.statItem} key={item.label}>
            <Statistic title={item.label} value={item.value} />
          </div>
        ))}
      </div>

      <ProTable<API.ClipTaskInfo>
        headerTitle="切片任务"
        actionRef={actionRef}
        rowKey="task_id"
        search={{ labelWidth: 72, defaultCollapsed: false }}
        pagination={{
          pageSize: 50,
          showSizeChanger: true,
          pageSizeOptions: ['20', '50', '100', '200'],
        }}
        size="middle"
        options={false}
        toolBarRender={() => [
          <Button key="reload" icon={<ReloadOutlined />} onClick={reload}>
            刷新
          </Button>,
        ]}
        request={async (params) => {
          try {
            const { current = 1, pageSize = 50, status, keyword } =
              params as any;
            const res = await listClipTask({
              page: current,
              page_size: pageSize,
              status,
              keyword,
            });
            return {
              data: res?.list || [],
              total: res?.total || 0,
              success: true,
            };
          } catch {
            return { data: [], total: 0, success: false };
          }
        }}
        columns={columns}
      />

      <Drawer
        title="切片任务详情"
        width={760}
        open={detailOpen}
        destroyOnClose
        onClose={() => setDetailOpen(false)}
      >
        {detail ? (
          <Space direction="vertical" size={16} className={styles.detailBody}>
            <Descriptions
              column={2}
              size="small"
              bordered
              items={[
                {
                  key: 'status',
                  label: '状态',
                  children: renderStatus(detail),
                },
                {
                  key: 'room',
                  label: '房间',
                  children: detail.room_name || '-',
                },
                {
                  key: 'record_file_count',
                  label: '来源文件数',
                  children: detail.record_file_count || 0,
                },
                {
                  key: 'clip_count',
                  label: '切片数',
                  children: detail.clip_count || 0,
                },
                {
                  key: 'created_at',
                  label: '创建时间',
                  children: formatDate(detail.created_at),
                },
                {
                  key: 'finished_at',
                  label: '结束时间',
                  children: formatDate(detail.finished_at),
                },
              ]}
            />

            {detail.error_message ? (
              <div className={styles.errorBlock}>{detail.error_message}</div>
            ) : null}

            <div>
              <div className={styles.sectionTitle}>
                <ScissorOutlined /> 探测区间
              </div>
              <Table<API.ClipTaskFileIntervals>
                rowKey={(row, index) =>
                  `${row.record_file_id || 'file'}-${index}`
                }
                size="small"
                pagination={false}
                columns={intervalColumns}
                dataSource={detail.intervals || []}
                locale={{
                  emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />,
                }}
              />
            </div>

            {detail.params ? (
              <div>
                <div className={styles.sectionTitle}>探测参数</div>
                <pre className={styles.jsonBlock}>
                  {formatJson(detail.params)}
                </pre>
              </div>
            ) : null}
          </Space>
        ) : detailLoading ? null : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
        )}
      </Drawer>
    </div>
  );
};

export default ClipTaskList;
