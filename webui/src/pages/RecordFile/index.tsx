import services from '@/services/luboman';
import {
  CloudUploadOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  ScissorOutlined,
} from '@ant-design/icons';
import {
  ActionType,
  ModalForm,
  ProColumns,
  ProFormSelect,
  ProFormText,
  ProTable,
} from '@ant-design/pro-components';
import { history } from '@umijs/max';
import {
  Alert,
  Badge,
  Button,
  Empty,
  Modal,
  Space,
  Spin,
  Tag,
  Tooltip,
  message,
} from 'antd';
import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import FlvPlayer from './FlvPlayer';
import styles from './index.less';

const {
  getRecordFileStreamUrl,
  listRecordFile,
  listRecordFileRoomSummary,
  publishRecordFileToBili,
  publishRecordFileToDouyin,
} = services.RecordFile;
const { detectDanceClip } = services.ClipTask;
const { listLiveRoom } = services.LiveRoom;
const { listBiliUploadTemplate } = services.BiliUploadTemplate;
const { listDouyinUploadTemplate } = services.DouyinUploadTemplate;

const ALL_ROOM_KEY = 'all';
type RoomFilterKey = typeof ALL_ROOM_KEY | number;
const RECORD_STATUS_RECORDING = 'RECORDING';
const RECORD_STATUS_COMPLETED = 'COMPLETED';

// 平台码 → 中文名（用于筛选项 + 来源展示）。Record 类型允许用字符串下标取值。
const PLATFORM_VALUE_ENUM: Record<string, { text: string }> = {
  douyin: { text: '抖音' },
  douyu: { text: '斗鱼' },
  huya: { text: '虎牙' },
  bilibili: { text: 'B站' },
  bili: { text: 'B站' },
  kuaishou: { text: '快手' },
  cc: { text: '网易CC' },
  afreecatv: { text: 'SOOP' },
  twitch: { text: 'Twitch' },
  youtube: { text: 'YouTube' },
};

/** 字节 → 人类可读 */
function humanSize(bytes?: number | null): string {
  if (bytes == null) return '-';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = bytes / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 10 ? 0 : 1)} ${units[i]}`;
}

function humanDuration(totalSeconds?: number | null): string {
  const seconds = Math.max(0, Math.floor(totalSeconds || 0));
  const hr = Math.floor(seconds / 3600);
  const min = Math.floor((seconds % 3600) / 60);
  const sec = seconds % 60;
  if (hr > 0) return `${hr}时${min.toString().padStart(2, '0')}分`;
  if (min > 0) return `${min}分${sec.toString().padStart(2, '0')}秒`;
  return `${sec}秒`;
}

/** 毫秒时间戳 → 相对时间 */
function relFromMs(ms: number): string {
  const diff = Date.now() - ms;
  if (diff < 0) return new Date(ms).toLocaleString();
  const min = Math.floor(diff / 60000);
  if (min < 1) return '刚刚';
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} 小时前`;
  const day = Math.floor(hr / 24);
  if (day < 30) return `${day} 天前`;
  return new Date(ms).toLocaleDateString();
}

function relFromDateValue(value?: string | null): string {
  if (!value) return '-';
  const ms = new Date(value).getTime();
  return Number.isNaN(ms) ? value : relFromMs(ms);
}

function boolFromFormValue(value: any, defaultValue: boolean): boolean {
  if (value === undefined || value === null) return defaultValue;
  if (typeof value === 'string') {
    return !['false', '0', 'no', 'none', ''].includes(
      value.trim().toLowerCase(),
    );
  }
  return Boolean(value);
}

function roomSummaryName(room: API.RecordFileRoomSummary): string {
  return (
    room.room_name ||
    (room.live_room_id ? `#${room.live_room_id}` : '未命名直播间')
  );
}

function isRecordCompleted(row: API.RecordFileInfo): boolean {
  return (row.status || RECORD_STATUS_COMPLETED) === RECORD_STATUS_COMPLETED;
}

/** 是否为探测切片产出的记录（series_code 以 CLIP: 开头标记来源录像 id） */
function isClipRecord(row: API.RecordFileInfo): boolean {
  return !!row.series_code && row.series_code.startsWith('CLIP:');
}

/** 浏览器原生 <video> 不支持 FLV，需走 flv.js；按路径后缀判断是否为 FLV。 */
function isFlvRecord(row?: API.RecordFileInfo): boolean {
  if (!row) return false;
  const path = row.stream_url || row.video || row.filename || '';
  return /\.flv(?:$|\?)/i.test(path);
}

/** 防御性渲染 upload_info：尝试取 bvid 拼链接，否则仅显示「已投稿」 */
function renderPublishStatus(info?: any) {
  if (!info) return <span className={styles.muted}>未投稿</span>;
  const bvid =
    info?.bvid || info?.data?.bvid || info?.result?.bvid || info?.avid_bvid;
  const tag = <Tag color="green">已投稿</Tag>;
  if (bvid) {
    return (
      <a
        href={`https://www.bilibili.com/video/${bvid}`}
        target="_blank"
        rel="noopener noreferrer"
      >
        {tag}
      </a>
    );
  }
  return tag;
}

const RecordFileList: React.FC = () => {
  const actionRef = useRef<ActionType>();
  const [roomSummary, setRoomSummary] = useState<API.RecordFileRoomSummary[]>(
    [],
  );
  const [roomsLoading, setRoomsLoading] = useState(false);
  const [activeRoomKey, setActiveRoomKey] =
    useState<RoomFilterKey>(ALL_ROOM_KEY);
  const [summaryExistsOnly, setSummaryExistsOnly] = useState(true);
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [selectedRows, setSelectedRows] = useState<API.RecordFileInfo[]>([]);
  const [publishOpen, setPublishOpen] = useState(false);
  const [publishDouyinOpen, setPublishDouyinOpen] = useState(false);
  const [publishTarget, setPublishTarget] = useState<API.RecordFileInfo[]>([]);
  const [playTarget, setPlayTarget] = useState<API.RecordFileInfo>();

  const activeRoomId =
    activeRoomKey === ALL_ROOM_KEY ? undefined : activeRoomKey;
  const tableParams = useMemo(
    () => ({ roomDimension: activeRoomKey }),
    [activeRoomKey],
  );
  const activeRoom = useMemo(
    () =>
      activeRoomId == null
        ? undefined
        : roomSummary.find((room) => room.live_room_id === activeRoomId),
    [activeRoomId, roomSummary],
  );
  const totalFileCount = useMemo(
    () => roomSummary.reduce((sum, room) => sum + (room.file_count || 0), 0),
    [roomSummary],
  );
  const hasRecordingSelection = selectedRows.some(
    (row) => !isRecordCompleted(row),
  );
  const playUrl = playTarget ? getRecordFileStreamUrl(playTarget) : undefined;

  const publishDefaultRoomId = useMemo(() => {
    if (activeRoomId != null) return activeRoomId;
    const roomIds = Array.from(
      new Set(
        publishTarget
          .map((row) => row.live_room_id)
          .filter((id): id is number => typeof id === 'number'),
      ),
    );
    return roomIds.length === 1 ? roomIds[0] : undefined;
  }, [activeRoomId, publishTarget]);

  const fetchRoomSummary = useCallback(async (existsOnly = true) => {
    setRoomsLoading(true);
    try {
      const list = await listRecordFileRoomSummary({
        exists_only: existsOnly,
      });
      setRoomSummary(Array.isArray(list) ? list : []);
    } catch {
      setRoomSummary([]);
    } finally {
      setRoomsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchRoomSummary(true);
  }, [fetchRoomSummary]);

  const openPublish = (rows: API.RecordFileInfo[]) => {
    if (!rows.length) return;
    setPublishTarget(rows);
    setPublishOpen(true);
  };

  const openPublishDouyin = (rows: API.RecordFileInfo[]) => {
    if (!rows.length) return;
    setPublishTarget(rows);
    setPublishDouyinOpen(true);
  };

  const openPlayer = (row: API.RecordFileInfo) => {
    if (row.id == null || !row.exists || !isRecordCompleted(row)) return;
    setPlayTarget(row);
  };

  const clearSelection = () => {
    setSelectedRowKeys([]);
    setSelectedRows([]);
  };

  const switchRoom = (roomKey: RoomFilterKey) => {
    setActiveRoomKey(roomKey);
    clearSelection();
  };

  const reloadRecordFiles = () => {
    fetchRoomSummary(summaryExistsOnly);
    actionRef.current?.reload();
  };

  const handleDetectDanceClip = () => {
    const target = selectedRows.filter(
      (row) => row.id != null && row.exists && isRecordCompleted(row),
    );
    if (!target.length) return;
    Modal.confirm({
      title: `探测三分屏舞蹈片段（${target.length} 个文件）`,
      content:
        '将检测所选录像中的三分屏（舞蹈）画面并自动切片，任务异步执行，可在「切片任务」页查看进度；切片产出会出现在文件管理列表中。',
      okText: '开始探测',
      cancelText: '取消',
      onOk: async () => {
        try {
          const res = await detectDanceClip({
            file_ids: target.map((row) => row.id as number),
          });
          clearSelection();
          Modal.success({
            title: '切片任务已创建',
            content: `任务 ID：${res.task_id}，共 ${res.file_count} 个文件。`,
            okText: '查看切片任务',
            onOk: () => history.push('/clipTask'),
          });
        } catch {
          // 统一错误层已弹 toast
        }
      },
    });
  };

  const handlePublish = async (values: any) => {
    const { bili_upload_template_ids, live_room_id, room_data } = values;
    const target = publishTarget;
    if (target.some((row) => !isRecordCompleted(row))) {
      message.warning('录制中的文件不能发布');
      return false;
    }
    // 后端单次调用只接受 file_ids 或 videos：全部有 id 用 file_ids，否则统一用 video 路径
    const allHaveId = target.length > 0 && target.every((r) => r.id != null);
    const roomData = room_data
      ? Object.fromEntries(
          Object.entries(room_data).filter(([, v]) => v != null && v !== ''),
        )
      : undefined;
    try {
      const res = await publishRecordFileToBili({
        bili_upload_template_ids,
        live_room_id,
        ...(allHaveId
          ? { file_ids: target.map((r) => r.id as number) }
          : { videos: target.map((r) => r.video) }),
        ...(roomData && Object.keys(roomData).length
          ? { room_data: roomData }
          : {}),
      });
      const taskCount = res.tasks?.length ?? 0;
      const errCount = res.errors?.length ?? 0;
      if (errCount > 0) {
        message.warning(
          `已创建 ${taskCount} 个投稿任务，${errCount} 个模板失败：${res.errors
            .map((e) => `模板 ${e.bili_upload_template_id}(${e.error})`)
            .join('；')}`,
        );
      } else {
        message.success(
          `已创建 ${taskCount} 个投稿任务（${bili_upload_template_ids.length} 个模板 × ${target.length} 个文件）`,
        );
      }
      clearSelection();
      actionRef.current?.reload();
      return true;
    } catch {
      // 统一错误层已弹 toast
      return false;
    }
  };

  const handlePublishDouyin = async (values: any) => {
    const { douyin_upload_template_ids, live_room_id, room_data } = values;
    const target = publishTarget;
    if (target.some((row) => !isRecordCompleted(row))) {
      message.warning('录制中的文件不能发布');
      return false;
    }
    // 后端单次调用只接受 file_ids 或 videos：全部有 id 用 file_ids，否则统一用 video 路径
    const allHaveId = target.length > 0 && target.every((r) => r.id != null);
    const roomData = room_data
      ? Object.fromEntries(
          Object.entries(room_data).filter(([, v]) => v != null && v !== ''),
        )
      : undefined;
    try {
      const res = await publishRecordFileToDouyin({
        douyin_upload_template_ids,
        live_room_id,
        ...(allHaveId
          ? { file_ids: target.map((r) => r.id as number) }
          : { videos: target.map((r) => r.video) }),
        ...(roomData && Object.keys(roomData).length
          ? { room_data: roomData }
          : {}),
      });
      const taskCount = res.tasks?.length ?? 0;
      const errCount = res.errors?.length ?? 0;
      if (errCount > 0) {
        message.warning(
          `已创建 ${taskCount} 个抖音投稿任务，${errCount} 个模板失败：${res.errors
            .map((e) => `模板 ${e.douyin_upload_template_id}(${e.error})`)
            .join('；')}`,
        );
      } else {
        message.success(
          `已创建 ${taskCount} 个抖音投稿任务（${douyin_upload_template_ids.length} 个模板 × ${target.length} 个文件）`,
        );
      }
      clearSelection();
      actionRef.current?.reload();
      return true;
    } catch {
      // 统一错误层已弹 toast
      return false;
    }
  };

  const columns: ProColumns<API.RecordFileInfo>[] = [
    // ---------- 仅筛选项（不在表格显示）----------
    {
      title: '平台',
      dataIndex: 'platform',
      valueType: 'select',
      valueEnum: PLATFORM_VALUE_ENUM,
      hideInTable: true,
    },
    {
      title: '关键词',
      dataIndex: 'keyword',
      valueType: 'text',
      hideInTable: true,
      fieldProps: { placeholder: '搜索文件名 / 房间名 / 路径' },
    },
    {
      title: '日期',
      dataIndex: 'date',
      valueType: 'date',
      hideInTable: true,
    },
    {
      title: '仅磁盘存在',
      dataIndex: 'exists_only',
      valueType: 'switch',
      hideInTable: true,
      formItemProps: { initialValue: true },
    },
    // ---------- 表格列 ----------
    {
      title: '文件名',
      dataIndex: 'filename',
      ellipsis: true,
      search: false,
      render: (_, r) => (
        <Tooltip title={r.video}>
          <span>{r.filename || r.video}</span>
        </Tooltip>
      ),
    },
    {
      title: '房间 / 平台',
      search: false,
      width: 170,
      render: (_, r) => (
        <Space direction="vertical" size={0}>
          <span>{r.room_name || <span className={styles.muted}>-</span>}</span>
          <Tag>
            {PLATFORM_VALUE_ENUM[r.room_platform || '']?.text ||
              r.room_platform ||
              '-'}
          </Tag>
        </Space>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 90,
      search: false,
      render: (_, r) =>
        r.status === RECORD_STATUS_RECORDING ? (
          <Tag color="processing">录制中</Tag>
        ) : (
          <Tag color="success">已完成</Tag>
        ),
    },
    {
      title: '录制时长',
      dataIndex: 'duration_seconds',
      width: 110,
      search: false,
      render: (_, r) => humanDuration(r.duration_seconds),
    },
    {
      title: '大小',
      dataIndex: 'size',
      width: 100,
      search: false,
      align: 'right',
      render: (_, r) => humanSize(r.size),
    },
    {
      title: '修改时间',
      dataIndex: 'mtime',
      width: 140,
      search: false,
      render: (_, r) =>
        r.mtime ? (
          <Tooltip title={new Date(r.mtime * 1000).toLocaleString()}>
            <span>{relFromMs(r.mtime * 1000)}</span>
          </Tooltip>
        ) : (
          '-'
        ),
    },
    {
      title: '来源',
      dataIndex: 'source',
      width: 110,
      search: false,
      render: (_, r) => (
        <Space size={4}>
          <Tag color={r.source === 'database' ? 'blue' : undefined}>
            {r.source === 'database' ? '数据库' : '磁盘'}
          </Tag>
          {isClipRecord(r) ? <Tag color="purple">切片</Tag> : null}
        </Space>
      ),
    },
    {
      title: '发布状态',
      dataIndex: 'upload_info',
      width: 100,
      search: false,
      render: (_, r) => renderPublishStatus(r.upload_info),
    },
    {
      title: '操作',
      valueType: 'option',
      width: 120,
      search: false,
      render: (_, r) => {
        const canOperate = r.id != null && r.exists && isRecordCompleted(r);
        if (!canOperate) {
          return <span className={styles.muted}>不可操作</span>;
        }
        return (
          <Space size={8}>
            <a onClick={() => openPlayer(r)}>播放</a>
            <a onClick={() => openPublish([r])}>发布</a>
            <a onClick={() => openPublishDouyin([r])}>投抖音</a>
          </Space>
        );
      },
    },
  ];

  return (
    <>
      <div className={styles.pageShell}>
        <aside className={styles.roomPanel}>
          <div className={styles.roomPanelHeader}>
            <span>直播间</span>
            {roomsLoading ? <Spin size="small" /> : null}
          </div>
          <div className={styles.roomList}>
            <button
              type="button"
              className={`${styles.roomItem} ${
                activeRoomKey === ALL_ROOM_KEY ? styles.roomItemActive : ''
              }`}
              onClick={() => switchRoom(ALL_ROOM_KEY)}
            >
              <div className={styles.roomItemTop}>
                <span className={styles.roomItemName}>全部录像</span>
                <Badge count={totalFileCount} overflowCount={9999} showZero />
              </div>
              <div className={styles.roomItemMeta}>
                {roomSummary.length} 个直播间
              </div>
            </button>
            {roomSummary.length ? (
              roomSummary
                .filter((room) => typeof room.live_room_id === 'number')
                .map((room) => {
                  const roomId = room.live_room_id as number;
                  const active = activeRoomKey === roomId;
                  return (
                    <button
                      key={roomId}
                      type="button"
                      className={`${styles.roomItem} ${
                        active ? styles.roomItemActive : ''
                      }`}
                      onClick={() => switchRoom(roomId)}
                    >
                      <div className={styles.roomItemTop}>
                        <span
                          className={styles.roomItemName}
                          title={roomSummaryName(room)}
                        >
                          {roomSummaryName(room)}
                        </span>
                        <Badge
                          count={room.file_count || 0}
                          overflowCount={9999}
                          showZero
                        />
                      </div>
                      <div className={styles.roomItemMeta}>
                        <span>
                          {PLATFORM_VALUE_ENUM[room.room_platform || '']
                            ?.text ||
                            room.room_platform ||
                            '未知平台'}
                        </span>
                        <span>{relFromDateValue(room.last_begin_time)}</span>
                      </div>
                    </button>
                  );
                })
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="暂无直播间"
              />
            )}
          </div>
        </aside>

        <div className={styles.tablePanel}>
          <ProTable<API.RecordFileInfo>
            headerTitle={
              activeRoom
                ? `${roomSummaryName(activeRoom)} · 录像文件`
                : '全部录像文件'
            }
            actionRef={actionRef}
            rowKey="video"
            params={tableParams}
            search={{ labelWidth: 80, defaultCollapsed: false }}
            pagination={{
              pageSize: 50,
              showSizeChanger: true,
              pageSizeOptions: ['20', '50', '100', '200'],
            }}
            size="middle"
            options={false}
            rowSelection={{
              selectedRowKeys,
              onChange: (keys, rows) => {
                setSelectedRowKeys(keys);
                setSelectedRows(rows);
              },
            }}
            tableAlertRender={({ selectedRowKeys: keys }) =>
              `已选 ${keys.length} 个文件`
            }
            tableAlertOptionRender={() => (
              <Space>
                <a onClick={clearSelection}>取消选择</a>
              </Space>
            )}
            toolBarRender={() => [
              <Button
                key="reload"
                icon={<ReloadOutlined />}
                onClick={reloadRecordFiles}
              >
                刷新
              </Button>,
              <Button
                key="play"
                icon={<PlayCircleOutlined />}
                disabled={
                  selectedRows.length !== 1 ||
                  selectedRows[0]?.id == null ||
                  !selectedRows[0]?.exists ||
                  !isRecordCompleted(selectedRows[0])
                }
                onClick={() => openPlayer(selectedRows[0])}
              >
                播放
              </Button>,
              <Button
                key="detect"
                icon={<ScissorOutlined />}
                disabled={
                  !selectedRows.length ||
                  hasRecordingSelection ||
                  selectedRows.some((row) => row.id == null || !row.exists)
                }
                title="检测所选录像中的三分屏（舞蹈）画面并自动切片"
                onClick={handleDetectDanceClip}
              >
                探测
                {selectedRows.length ? ` (${selectedRows.length})` : ''}
              </Button>,
              <Button
                key="publish"
                type="primary"
                icon={<CloudUploadOutlined />}
                disabled={!selectedRows.length || hasRecordingSelection}
                title={
                  hasRecordingSelection ? '录制中的文件不能发布' : undefined
                }
                onClick={() => openPublish(selectedRows)}
              >
                发布到 B 站
                {selectedRows.length ? ` (${selectedRows.length})` : ''}
              </Button>,
              <Button
                key="publishDouyin"
                icon={<CloudUploadOutlined />}
                disabled={!selectedRows.length || hasRecordingSelection}
                title={
                  hasRecordingSelection ? '录制中的文件不能发布' : undefined
                }
                onClick={() => openPublishDouyin(selectedRows)}
              >
                发布到抖音
                {selectedRows.length ? ` (${selectedRows.length})` : ''}
              </Button>,
            ]}
            request={async (params) => {
              try {
                const {
                  current = 1,
                  pageSize = 50,
                  platform,
                  keyword,
                  date,
                  exists_only,
                } = params as any;
                const effectiveExistsOnly = boolFromFormValue(
                  exists_only,
                  true,
                );
                if (effectiveExistsOnly !== summaryExistsOnly) {
                  setSummaryExistsOnly(effectiveExistsOnly);
                  fetchRoomSummary(effectiveExistsOnly);
                }
                const dateStr =
                  date && typeof date.format === 'function'
                    ? date.format('YYYY-MM-DD')
                    : date || undefined;
                const res = await listRecordFile({
                  page: current,
                  page_size: pageSize,
                  live_room_id: activeRoomId,
                  platform,
                  keyword,
                  date: dateStr,
                  exists_only: effectiveExistsOnly,
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
        </div>
      </div>

      <Modal
        title={playTarget?.filename || '在线播放'}
        width={900}
        open={!!playTarget}
        footer={null}
        destroyOnClose
        onCancel={() => setPlayTarget(undefined)}
      >
        {playUrl ? (
          isFlvRecord(playTarget) ? (
            <FlvPlayer url={playUrl} />
          ) : (
            <video
              className={styles.player}
              src={playUrl}
              controls
              playsInline
              preload="metadata"
            />
          )
        ) : null}
        <div className={styles.playerMeta}>{playTarget?.video || ''}</div>
      </Modal>

      <ModalForm
        title={`发布到 B 站（${publishTarget.length} 个文件）`}
        width={560}
        open={publishOpen}
        onOpenChange={setPublishOpen}
        modalProps={{ destroyOnClose: true }}
        onFinish={handlePublish}
        initialValues={{ live_room_id: publishDefaultRoomId }}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="投稿为异步任务，提交后立即返回任务 ID；上传完成后稍后刷新列表即可在「发布状态」查看结果。"
        />
        <ProFormSelect
          name="bili_upload_template_ids"
          label="投稿模板"
          rules={[{ required: true, message: '请至少选择一个投稿模板' }]}
          request={async () => {
            const list = await listBiliUploadTemplate();
            return (list || []).map((t) => ({
              label: t.template_name,
              value: t.id,
            }));
          }}
          placeholder="选择投稿模板（可多选，每个模板绑定一个 B 站账号）"
          fieldProps={{ mode: 'multiple' }}
        />
        <ProFormSelect
          name="live_room_id"
          label="关联直播间"
          allowClear
          request={async () => {
            const list = await listLiveRoom();
            return (list || []).map((r) => ({
              label: r.room_name || `#${r.id}`,
              value: r.id,
            }));
          }}
          placeholder="可选，用于回填分区 / 标签等上下文"
        />
        <div className={styles.overrideTitle}>
          覆盖信息（可选，留空则用模板/房间默认值）
        </div>
        <ProFormText
          name={['room_data', 'room_name']}
          label="房间名"
          placeholder="覆盖稿件关联的房间名"
        />
        <ProFormText
          name={['room_data', 'room_title']}
          label="房间标题"
          placeholder="覆盖房间标题"
        />
        <ProFormText
          name={['room_data', 'room_owner']}
          label="主播"
          placeholder="覆盖主播名"
        />
        <ProFormSelect
          name={['room_data', 'room_platform']}
          label="平台"
          valueEnum={PLATFORM_VALUE_ENUM}
          placeholder="覆盖平台"
          allowClear
        />
      </ModalForm>

      <ModalForm
        title={`发布到抖音（${publishTarget.length} 个文件）`}
        width={560}
        open={publishDouyinOpen}
        onOpenChange={setPublishDouyinOpen}
        modalProps={{ destroyOnClose: true }}
        onFinish={handlePublishDouyin}
        initialValues={{ live_room_id: publishDefaultRoomId }}
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message="抖音限 ≤4G / ≤15 分钟（超限会被拦截）；切片按模板配置自动裁竖屏。查重严格，勿将同一切片投多个抖音号。"
        />
        <ProFormSelect
          name="douyin_upload_template_ids"
          label="投稿模板"
          rules={[{ required: true, message: '请至少选择一个抖音投稿模板' }]}
          request={async () => {
            const list = await listDouyinUploadTemplate();
            return (list || []).map((t) => ({
              label: t.template_name,
              value: t.id,
            }));
          }}
          placeholder="选择抖音投稿模板（可多选，每个模板绑定一个抖音账号）"
          fieldProps={{ mode: 'multiple' }}
        />
        <ProFormSelect
          name="live_room_id"
          label="关联直播间"
          allowClear
          request={async () => {
            const list = await listLiveRoom();
            return (list || []).map((r) => ({
              label: r.room_name || `#${r.id}`,
              value: r.id,
            }));
          }}
          placeholder="可选，用于回填标题上下文"
        />
        <div className={styles.overrideTitle}>
          覆盖信息（可选，留空则用模板/房间默认值）
        </div>
        <ProFormText
          name={['room_data', 'room_name']}
          label="房间名"
          placeholder="覆盖稿件关联的房间名"
        />
        <ProFormText
          name={['room_data', 'room_title']}
          label="房间标题"
          placeholder="覆盖房间标题"
        />
        <ProFormText
          name={['room_data', 'room_owner']}
          label="主播"
          placeholder="覆盖主播名"
        />
      </ModalForm>
    </>
  );
};

export default RecordFileList;
