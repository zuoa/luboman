import services from '@/services/luboman';
import {
  CloudUploadOutlined,
  FileSearchOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { ActionType, ProColumns, ProTable } from '@ant-design/pro-components';
import {
  Button,
  Descriptions,
  Drawer,
  Empty,
  Space,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import styles from './index.less';

const { listSubmissionTask, getSubmissionTask, getSubmissionTaskStats } =
  services.SubmissionTask;

const STATUS_VALUE_ENUM = {
  PENDING: { text: '排队中' },
  RUNNING: { text: '上传中' },
  RETRYING: { text: '待重试' },
  SUCCESS: { text: '成功' },
  FAILED: { text: '失败' },
};

const STATUS_META: Record<string, { text: string; color: string }> = {
  PENDING: { text: '排队中', color: 'default' },
  RUNNING: { text: '上传中', color: 'processing' },
  RETRYING: { text: '待重试', color: 'warning' },
  SUCCESS: { text: '成功', color: 'success' },
  FAILED: { text: '失败', color: 'error' },
};

const SOURCE_VALUE_ENUM = {
  AUTO: { text: '自动投稿' },
  FILE_MANAGER: { text: '文件管理' },
};

const SOURCE_LABELS: Record<string, string> = {
  AUTO: '自动投稿',
  FILE_MANAGER: '文件管理',
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

function renderStatus(row: API.SubmissionTaskInfo) {
  const meta = STATUS_META[row.status || ''] || {
    text: row.status || '-',
    color: 'default',
  };
  return (
    <Space size={6}>
      <Tag color={meta.color}>{meta.text}</Tag>
      {row.status === 'RETRYING' ? (
        <span className={styles.muted}>
          {row.retry_count || 0}/{row.max_retries || 0}
        </span>
      ) : null}
    </Space>
  );
}

function fileName(path?: string): string {
  if (!path) return '-';
  const parts = path.split('/');
  return parts[parts.length - 1] || path;
}

const SubmissionTaskList: React.FC = () => {
  const actionRef = useRef<ActionType>();
  const [stats, setStats] = useState<API.SubmissionTaskStats>();
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detail, setDetail] = useState<API.SubmissionTaskInfo>();

  const fetchStats = useCallback(async () => {
    try {
      setStats(await getSubmissionTaskStats());
    } catch {
      setStats(undefined);
    }
  }, []);

  useEffect(() => {
    fetchStats();
  }, [fetchStats]);

  const reload = () => {
    fetchStats();
    actionRef.current?.reload();
  };

  const openDetail = async (row: API.SubmissionTaskInfo) => {
    setDetailOpen(true);
    setDetail(row);
    setDetailLoading(true);
    try {
      setDetail(await getSubmissionTask({ task_id: row.task_id }));
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

  const columns: ProColumns<API.SubmissionTaskInfo>[] = [
    {
      title: '状态',
      dataIndex: 'status',
      valueType: 'select',
      valueEnum: STATUS_VALUE_ENUM,
      width: 110,
      render: (_, row) => renderStatus(row),
    },
    {
      title: '来源',
      dataIndex: 'source',
      valueType: 'select',
      valueEnum: SOURCE_VALUE_ENUM,
      width: 110,
      render: (_, row) => SOURCE_LABELS[row.source] || row.source || '-',
    },
    {
      title: '关键词',
      dataIndex: 'keyword',
      valueType: 'text',
      hideInTable: true,
      fieldProps: { placeholder: '任务 ID / 房间 / 模板 / 上传器' },
    },
    {
      title: '上传器',
      dataIndex: 'platform',
      width: 110,
      render: (_, row) => <Tag>{row.uploader || row.platform || '-'}</Tag>,
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
      title: '房间 / 模板',
      search: false,
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <span>
            {row.room_name || <span className={styles.muted}>-</span>}
          </span>
          <span className={styles.muted}>
            {row.bili_upload_template_name || '未命名模板'}
          </span>
        </Space>
      ),
    },
    {
      title: '文件数',
      dataIndex: 'file_count',
      width: 86,
      search: false,
      align: 'right',
      render: (_, row) => row.file_count || row.file_list?.length || 0,
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
      width: 80,
      render: (_, row) => <a onClick={() => openDetail(row)}>详情</a>,
    },
  ];

  const fileColumns = [
    {
      title: '文件',
      dataIndex: 'video',
      render: (value: string) => (
        <Tooltip title={value}>
          <span className={styles.filePath}>{fileName(value)}</span>
        </Tooltip>
      ),
    },
    {
      title: '录像 ID',
      dataIndex: 'id',
      width: 100,
      render: (_: any, row: API.SubmissionTaskFile) =>
        row.id || row.record_file_id || '-',
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

      <ProTable<API.SubmissionTaskInfo>
        headerTitle="投稿任务"
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
            const {
              current = 1,
              pageSize = 50,
              status,
              source,
              platform,
              keyword,
            } = params as any;
            const res = await listSubmissionTask({
              page: current,
              page_size: pageSize,
              status,
              source,
              platform,
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
        title="投稿任务详情"
        width={720}
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
                  key: 'source',
                  label: '来源',
                  children: SOURCE_LABELS[detail.source] || detail.source,
                },
                {
                  key: 'uploader',
                  label: '上传器',
                  children: detail.uploader || detail.platform,
                },
                {
                  key: 'file_count',
                  label: '文件数',
                  children: detail.file_count || 0,
                },
                {
                  key: 'room',
                  label: '房间',
                  children: detail.room_name || '-',
                },
                {
                  key: 'template',
                  label: '投稿模板',
                  children: detail.bili_upload_template_name || '-',
                },
                {
                  key: 'created_at',
                  label: '创建时间',
                  children: formatDate(detail.created_at),
                },
                {
                  key: 'started_at',
                  label: '开始时间',
                  children: formatDate(detail.started_at),
                },
                {
                  key: 'finished_at',
                  label: '结束时间',
                  children: formatDate(detail.finished_at),
                },
                {
                  key: 'retry',
                  label: '重试',
                  children: `${detail.retry_count || 0}/${
                    detail.max_retries || 0
                  }`,
                },
              ]}
            />

            {detail.error_message ? (
              <div className={styles.errorBlock}>{detail.error_message}</div>
            ) : null}

            <Table<API.SubmissionTaskFile>
              rowKey={(row, index) => `${row.video || 'file'}-${index}`}
              size="small"
              pagination={false}
              columns={fileColumns}
              dataSource={detail.file_list || []}
              locale={{
                emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />,
              }}
            />

            {detail.result ? (
              <div>
                <div className={styles.sectionTitle}>
                  <CloudUploadOutlined /> 上传结果
                </div>
                <pre className={styles.jsonBlock}>
                  {formatJson(detail.result)}
                </pre>
              </div>
            ) : null}

            {detail.metadata ? (
              <div>
                <div className={styles.sectionTitle}>
                  <FileSearchOutlined /> 元数据
                </div>
                <pre className={styles.jsonBlock}>
                  {formatJson(detail.metadata)}
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

export default SubmissionTaskList;
