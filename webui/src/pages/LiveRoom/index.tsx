import services from '@/services/luboman';
import { REQUEST_HOST } from '@/constants';
import {
  ModalForm,
  ProFormDateTimePicker,
  ProFormDependency,
  ProFormRadio,
  ProFormSelect,
  ProFormSwitch,
  ProFormText,
} from '@ant-design/pro-components';
import {
  AppstoreOutlined,
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
  TableOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import {
  Avatar,
  Badge,
  Button,
  Col,
  Descriptions,
  Empty,
  Form,
  Popconfirm,
  Row,
  Segmented,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Upload,
  message,
} from 'antd';
import React, { useCallback, useEffect, useState } from 'react';
import defaultRoomImg from '../../assets/default.jpg';
import styles from './index.less';

const { listLiveRoom, addLiveRoom, updateLiveRoom, deleteLiveRoom } =
  services.LiveRoom;
const { probeLiveRoom } = services.LiveRoom;
const { listBiliUploadTemplate } = services.BiliUploadTemplate;
const { listDouyinUploadTemplate } = services.DouyinUploadTemplate;
const { uploadCover } = services.Upload;

const VIEW_KEY = 'luboman_liveRoomView';
type ViewMode = 'table' | 'card';

const STORAGE_PLATFORM_OPTIONS = [
  { label: '不上传', value: '' },
  { label: '百度网盘', value: 'bdpan' },
  { label: '阿里云盘', value: 'alipan' },
  { label: '夸克网盘', value: 'quark' },
];

const VIDEO_FORMAT_OPTIONS = [
  { label: 'flv', value: 'flv' },
  { label: 'ts', value: 'ts' },
  { label: 'fmp4', value: 'fmp4' },
];

// 网盘平台 → 展示名
const STORAGE_LABELS: Record<string, string> = {
  bdpan: '百度网盘',
  alipan: '阿里云盘',
  quark: '夸克网盘',
};

// B 站投稿封面模式（空值 = 跟随投稿模板的 cover_path，保持旧行为）
const COVER_MODE_OPTIONS = [
  { label: '跟随模板', value: '' },
  { label: '自定义上传', value: 'custom' },
  { label: '最近直播封面', value: 'latest_live' },
  { label: '不设置', value: 'none' },
];

// 直播平台 → Tag 配色（未命中走默认中性色）
const PLATFORM_COLORS: Record<string, string> = {
  douyin: 'cyan',
  douyu: 'orange',
  huya: 'red',
  bili: 'blue',
  bilibili: 'blue',
  twitch: 'purple',
  afreecatv: 'magenta',
  youtube: 'red',
};

// 平台存储值 → 显示名（存储键保持旧值以兼容存量数据）
const PLATFORM_LABELS: Record<string, string> = {
  afreecatv: 'SOOP',
};

/** wsrv.nl 图片代理：规避防盗链 + 转 webp */
const proxyImg = (url?: string, w = 320, h = 180) =>
  url ? `https://wsrv.nl/?url=${encodeURIComponent(url)}&w=${w}&h=${h}&output=webp` : '';

/** 后端时间字符串 → 相对时间，无法解析则原样返回 */
function formatRelative(value?: string): string {
  if (!value) return '';
  const t = new Date(value).getTime();
  if (Number.isNaN(t)) return value;
  const diff = Date.now() - t;
  if (diff < 0) return value; // 未来时间原样显示
  const min = Math.floor(diff / 60000);
  if (min < 1) return '刚刚';
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} 小时前`;
  const day = Math.floor(hr / 24);
  if (day < 30) return `${day} 天前`;
  return value.length > 16 ? `${value.slice(0, 16)}…` : value;
}

/**
 * 直播间链接「探测」按钮：调用后端按链接抓取平台/房间名/开播状态，
 * 自动回填房间名。挂在输入框 suffix 上，须在 Form 上下文内渲染。
 */
const ProbeButton: React.FC = () => {
  const form = Form.useFormInstance();
  const [probing, setProbing] = useState(false);

  const handleProbe = async () => {
    const url = (form.getFieldValue('room_url') || '').trim();
    if (!url) {
      message.warning('请先输入直播间链接');
      return;
    }
    setProbing(true);
    try {
      const info = await probeLiveRoom(url);
      if (info.room_name) {
        form.setFieldsValue({ room_name: info.room_name });
      }
      if (info.live_state === 1) {
        message.success(
          `探测成功：${info.room_platform}${
            info.room_title ? `｜${info.room_title}` : ''
          }`,
        );
      } else if (info.room_name) {
        message.info('主播当前未开播，已回填房间名');
      } else {
        message.warning(
          `已识别平台：${info.room_platform}；主播未开播，暂无法获取房间名，请手动填写`,
        );
      }
    } catch {
      // 统一错误层已弹 toast
    } finally {
      setProbing(false);
    }
  };

  return (
    <Button
      type="link"
      size="small"
      icon={<SearchOutlined />}
      loading={probing}
      onClick={handleProbe}
      style={{ paddingInline: 4 }}
    >
      探测
    </Button>
  );
};

/**
 * 自定义封面上传：成功后把服务器路径写入隐藏的 custom_cover_path 字段并预览。
 * 编辑已有记录时按文件名推导预览地址（自定义封面固定落盘 {public}/cover/custom/）。
 */
const CoverUploadField: React.FC = () => {
  const form = Form.useFormInstance();
  const coverPath = Form.useWatch('custom_cover_path', form);
  const [uploading, setUploading] = useState(false);
  const [previewUrl, setPreviewUrl] = useState<string>();

  const shownUrl =
    previewUrl ||
    (coverPath
      ? `${REQUEST_HOST}/public/cover/custom/${coverPath.split('/').pop()}`
      : '');

  return (
    <Form.Item label="封面图片" required>
      <Space direction="vertical" size={8}>
        <Space size={8}>
          <Upload
            accept=".jpg,.jpeg,.png,.webp"
            showUploadList={false}
            customRequest={async ({ file, onSuccess, onError }) => {
              setUploading(true);
              try {
                const res = await uploadCover(file as File);
                form.setFieldsValue({ custom_cover_path: res.path });
                setPreviewUrl(`${REQUEST_HOST}${res.url}`);
                onSuccess?.(res);
                message.success('封面已上传');
              } catch (e) {
                // 统一错误层已弹 toast
                onError?.(e as Error);
              } finally {
                setUploading(false);
              }
            }}
          >
            <Button loading={uploading} icon={<UploadOutlined />}>
              {coverPath ? '重新上传' : '上传封面'}
            </Button>
          </Upload>
          {coverPath ? (
            <Button
              size="small"
              type="link"
              onClick={() => {
                form.setFieldsValue({ custom_cover_path: null });
                setPreviewUrl(undefined);
              }}
            >
              移除
            </Button>
          ) : null}
        </Space>
        {shownUrl ? (
          <img
            src={shownUrl}
            alt="封面预览"
            style={{ width: 240, borderRadius: 6, display: 'block' }}
          />
        ) : (
          <span style={{ color: 'rgba(255,255,255,0.45)', fontSize: 13 }}>
            未上传封面时将回退使用投稿模板的封面
          </span>
        )}
      </Space>
    </Form.Item>
  );
};

/** 最近直播封面预览：开播时后端自动缓存，未开播过则提示 */
const LiveCoverPreview: React.FC = () => {
  const form = Form.useFormInstance();
  const coverUrl = form.getFieldValue('room_cover_url');
  return (
    <Form.Item label="封面预览">
      {coverUrl ? (
        <img
          src={proxyImg(coverUrl)}
          alt="最近直播封面"
          style={{ width: 240, borderRadius: 6, display: 'block' }}
        />
      ) : (
        <span style={{ color: 'rgba(255,255,255,0.45)', fontSize: 13 }}>
          暂无直播封面缓存，开播后自动获取；获取失败时回退使用投稿模板的封面
        </span>
      )}
    </Form.Item>
  );
};

/**
 * 后端 update_live_room 仅更新以下字段（白名单合并）：
 * room_name, room_url, custom_filename, bili_upload_template_id, bili_upload_template_ids,
 * upload_storage_platform, stream_video_format, active_state, active_begin, active_end,
 * auto_dance_clip, cover_mode, custom_cover_path。
 * 故新建/编辑表单仅暴露这些可编辑字段；投稿模板为多选（一份录播可投多个账号）。
 */
const RoomFormFields: React.FC = () => (
  <>
    <ProFormText
      name="room_url"
      label="直播间链接"
      placeholder="https://..."
      rules={[{ required: true, message: '请输入直播间链接' }]}
      fieldProps={{ suffix: <ProbeButton /> }}
    />
    <ProFormText name="room_name" label="直播间名称" placeholder="直播间名称" />
    <ProFormText
      name="custom_filename"
      label="自定义文件名"
      placeholder="{room_name}.%Y_%m_%d_%H_%M_%S.{title}"
      extra="支持 {room_name} {title} 与 strftime 时间变量"
    />
    <ProFormSelect
      name="bili_upload_template_ids"
      label="B 站投稿模板"
      request={async () => {
        const list = await listBiliUploadTemplate();
        return (list || []).map((t) => ({
          label: t.template_name,
          value: t.id,
        }));
      }}
      placeholder="选择 B 站投稿模板（可多选，每个模板绑定一个账号）"
      fieldProps={{ mode: 'multiple' }}
      allowClear
    />
    <ProFormSelect
      name="douyin_upload_template_ids"
      label="抖音投稿模板"
      request={async () => {
        const list = await listDouyinUploadTemplate();
        return (list || []).map((t) => ({
          label: t.template_name,
          value: t.id,
        }));
      }}
      placeholder="选择抖音投稿模板（可多选）"
      extra="只支持切片投稿（如舞蹈切片），不支持完整录播；抖音查重严格：同一切片投多个抖音号易判搬运限流，建议只选一个模板"
      fieldProps={{ mode: 'multiple' }}
      allowClear
    />
    <ProFormSelect
      name="upload_storage_platform"
      label="网盘上传"
      options={STORAGE_PLATFORM_OPTIONS}
      placeholder="选择网盘（可选）"
    />
    <ProFormSelect
      name="stream_video_format"
      label="流封装格式"
      options={VIDEO_FORMAT_OPTIONS}
      placeholder="flv"
    />
    <ProFormRadio.Group
      name="cover_mode"
      label="投稿封面"
      options={COVER_MODE_OPTIONS}
      extra="仅作用于 B 站投稿：不设置 = 完全不传封面（B 站自动截帧）；跟随模板 = 使用投稿模板里配置的封面路径"
    />
    <ProFormDependency name={['cover_mode']}>
      {({ cover_mode }) => {
        if (cover_mode === 'custom') {
          return (
            <>
              <Form.Item name="custom_cover_path" hidden />
              <CoverUploadField />
            </>
          );
        }
        if (cover_mode === 'latest_live') {
          return <LiveCoverPreview />;
        }
        return null;
      }}
    </ProFormDependency>
    <ProFormSwitch
      name="active_state"
      label="激活状态"
    />
    <ProFormSwitch
      name="auto_dance_clip"
      label="自动舞蹈切片"
      tooltip="每个录制分段完成后自动探测三分屏舞蹈片段并切片；跨分段的舞蹈会自动拼接；配置投稿模板后切片会逐个单独投稿到 B 站"
    />
    <ProFormDateTimePicker name="active_begin" label="激活起始时间" />
    <ProFormDateTimePicker name="active_end" label="激活截止时间" />
  </>
);

const LiveRoomList: React.FC = () => {
  const [list, setList] = useState<API.LiveRoomInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [templateMap, setTemplateMap] = useState<Record<number, string>>({});
  const [douyinTemplateMap, setDouyinTemplateMap] = useState<Record<number, string>>({});
  // 视图模式持久化：默认表格，记忆上次选择
  const [view, setView] = useState<ViewMode>(() =>
    localStorage.getItem(VIEW_KEY) === 'card' ? 'card' : 'table',
  );
  const [createOpen, setCreateOpen] = useState(false);
  const [editing, setEditing] = useState<API.LiveRoomInfo>();

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listLiveRoom();
      setList(Array.isArray(data) ? data : []);
    } catch {
      // 统一错误层已弹 toast
      setList([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
    // 拉取投稿模板名映射，供表格/卡片展示
    listBiliUploadTemplate()
      .then((list) => {
        const map: Record<number, string> = {};
        (list || []).forEach((t) => {
          if (t.id != null) map[t.id] = t.template_name;
        });
        setTemplateMap(map);
      })
      .catch(() => {});
    listDouyinUploadTemplate()
      .then((list) => {
        const map: Record<number, string> = {};
        (list || []).forEach((t) => {
          if (t.id != null) map[t.id] = t.template_name;
        });
        setDouyinTemplateMap(map);
      })
      .catch(() => {});
  }, [fetchData]);

  const changeView = (v: ViewMode) => {
    setView(v);
    localStorage.setItem(VIEW_KEY, v);
  };

  const handleDelete = async (id: number) => {
    await deleteLiveRoom(id);
    fetchData();
  };

  // 投稿模板名列表（多模板，带回退：旧数据只有单模板字段）
  const templateLabels = (r: API.LiveRoomInfo): string[] => {
    const ids =
      r.bili_upload_template_ids && r.bili_upload_template_ids.length > 0
        ? r.bili_upload_template_ids
        : r.bili_upload_template_id != null
          ? [r.bili_upload_template_id]
          : [];
    return ids.map((id) => templateMap[id] || `投稿 #${id}`);
  };

  const douyinTemplateLabels = (r: API.LiveRoomInfo): string[] =>
    (r.douyin_upload_template_ids || []).map(
      (id) => douyinTemplateMap[id] || `抖音投稿 #${id}`,
    );

  const toolbar = (
    <div className={styles.toolbar}>
      <span className={styles.title}>直播间管理</span>
      <Space>
        <Segmented
          value={view}
          onChange={(v) => changeView(v as ViewMode)}
          options={[
            { label: '表格', value: 'table', icon: <TableOutlined /> },
            { label: '卡片', value: 'card', icon: <AppstoreOutlined /> },
          ]}
        />
        <Button icon={<ReloadOutlined />} loading={loading} onClick={fetchData}>
          刷新
        </Button>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => setCreateOpen(true)}
        >
          新建
        </Button>
      </Space>
    </div>
  );

  return (
    <>
      {toolbar}

      {view === 'table' ? (
        <Table<API.LiveRoomInfo>
          rowKey="id"
          dataSource={list}
          loading={loading}
          pagination={false}
          size="middle"
          expandable={{
            rowExpandable: (r) =>
              !!(r.custom_filename || r.active_begin || r.active_end),
            expandedRowRender: (r) => (
              <Descriptions
                size="small"
                column={{ xs: 1, sm: 2, md: 3 }}
                colon={false}
                style={{ marginLeft: 46 }}
                labelStyle={{ color: 'var(--lb-text-tertiary)', width: 96 }}
              >
                <Descriptions.Item label="ID">{r.id ?? '-'}</Descriptions.Item>
                <Descriptions.Item label="流封装格式">
                  {r.stream_video_format || 'flv'}
                </Descriptions.Item>
                <Descriptions.Item label="激活时段">
                  {r.active_begin || '—'} ~ {r.active_end || '—'}
                </Descriptions.Item>
                <Descriptions.Item label="自定义文件名" span={3}>
                  <code className={styles.codeInline}>
                    {r.custom_filename || '-'}
                  </code>
                </Descriptions.Item>
              </Descriptions>
            ),
          }}
          columns={[
            {
              title: '直播间',
              dataIndex: 'room_name',
              render: (_, r) => (
                <div className={styles.roomCell}>
                  <Avatar
                    src={proxyImg(r.room_owner_avatar, 64, 64) || undefined}
                    size={34}
                  >
                    {(r.room_name || '?').slice(0, 1)}
                  </Avatar>
                  <div className={styles.roomCellInfo}>
                    <a
                      href={r.room_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className={styles.roomCellName}
                      title={r.room_name || ''}
                    >
                      {r.room_name || r.room_url}
                    </a>
                    <div
                      className={styles.roomCellSub}
                      title={r.room_title || r.room_owner || ''}
                    >
                      {r.room_title ||
                        (r.room_owner ? `@${r.room_owner}` : '')}
                    </div>
                  </div>
                </div>
              ),
            },
            {
              title: '平台',
              dataIndex: 'room_platform',
              width: 90,
              render: (_, r) =>
                r.room_platform ? (
                  <Tag color={PLATFORM_COLORS[r.room_platform!.toLowerCase()]}>
                    {PLATFORM_LABELS[r.room_platform!.toLowerCase()] || r.room_platform}
                  </Tag>
                ) : (
                  <span className={styles.muted}>-</span>
                ),
            },
            {
              title: '状态',
              width: 150,
              render: (_, r) => (
                <Space size={4} wrap>
                  <Badge
                    status={r.live_state === 1 ? 'error' : 'default'}
                    text={r.live_state === 1 ? '直播中' : '未开播'}
                  />
                  {r.status === 'WORKING' && (
                    <Tag color="processing">录制中</Tag>
                  )}
                </Space>
              ),
            },
            {
              title: '投稿 / 网盘',
              width: 180,
              render: (_, r) => {
                const tmpls = templateLabels(r);
                const douyinTmpls = douyinTemplateLabels(r);
                const storage = r.upload_storage_platform
                  ? STORAGE_LABELS[r.upload_storage_platform] ||
                    r.upload_storage_platform
                  : null;
                if (!tmpls.length && !douyinTmpls.length && !storage)
                  return <span className={styles.muted}>-</span>;
                return (
                  <Space size={4} wrap>
                    {tmpls.map((name) => (
                      <Tag key={name} color="blue">
                        {name}
                      </Tag>
                    ))}
                    {douyinTmpls.map((name) => (
                      <Tag key={name} color="magenta">
                        {name}
                      </Tag>
                    ))}
                    {storage && <Tag color="geekblue">{storage}</Tag>}
                  </Space>
                );
              },
            },
            {
              title: '最后直播',
              dataIndex: 'last_living_time',
              width: 130,
              render: (_, r) => {
                const rel = formatRelative(r.last_living_time);
                return rel ? (
                  <Tooltip title={r.last_living_time}>
                    <span className={styles.muted}>{rel}</span>
                  </Tooltip>
                ) : (
                  <span className={styles.muted}>从未</span>
                );
              },
            },
            {
              title: '激活',
              dataIndex: 'active_state',
              width: 84,
              render: (_, r) => (
                <Badge
                  status={r.active_state === 1 ? 'success' : 'warning'}
                  text={r.active_state === 1 ? '已激活' : '未激活'}
                />
              ),
            },
            {
              title: '操作',
              key: 'option',
              width: 96,
              render: (_, r) => (
                <Space>
                  <a onClick={() => setEditing(r)}>编辑</a>
                  <Popconfirm
                    title={`确认删除「${r.room_name || r.room_url}」？`}
                    description="删除后将停止录制"
                    onConfirm={() => r.id && handleDelete(r.id)}
                  >
                    <a className={styles.dangerLink}>删除</a>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      ) : loading ? (
        <div className={styles.center}>
          <Spin size="large" />
        </div>
      ) : !list.length ? (
        <Empty description="暂无直播间" style={{ marginTop: 48 }} />
      ) : (
        <Row gutter={[16, 16]} className={styles.cardsRow}>
          {list.map((r) => {
            const isLive = r.live_state === 1;
            const recording = r.status === 'WORKING';
            return (
              <Col key={r.id ?? r.room_url} xs={24} sm={12} md={8} lg={8} xl={6}>
                <div className={styles.card}>
                  <div className={styles.imageContainer}>
                    <img
                      src={proxyImg(r.room_cover_url, 400, 225) || defaultRoomImg}
                      alt={r.room_name}
                      style={{ opacity: isLive ? 1 : 0.35 }}
                      onError={(e) => {
                        e.currentTarget.onerror = null;
                        e.currentTarget.src = defaultRoomImg;
                      }}
                    />
                    <div className={styles.liveStatusBadge}>
                      <span
                        className={`${styles.liveStatus} ${
                          isLive ? styles.live : styles.offline
                        }`}
                      >
                        {isLive ? '直播中' : '未开播'}
                      </span>
                    </div>
                    <div className={styles.platformBadge}>
                      <span className={styles.platformTag}>
                        {(r.room_platform && PLATFORM_LABELS[r.room_platform.toLowerCase()]) || r.room_platform || '未知'}
                      </span>
                    </div>
                    {recording && (
                      <div className={styles.recBadge}>录制中</div>
                    )}
                  </div>

                  <div className={styles.roomInfo}>
                    <div className={styles.streamerInfo}>
                      <Avatar
                        src={proxyImg(r.room_owner_avatar, 64, 64) || undefined}
                        size={32}
                      >
                        {(r.room_name || '?').slice(0, 1)}
                      </Avatar>
                      <div className={styles.roomDetails}>
                        <div className={styles.roomName}>
                          <a
                            href={r.room_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            title={r.room_name}
                          >
                            {r.room_name || r.room_url}
                          </a>
                        </div>
                        <div
                          className={styles.roomTitle}
                          title={r.room_title || ''}
                        >
                          {r.room_title || '暂无标题'}
                        </div>
                        <div className={styles.lastLiveTime}>
                          {isLive
                            ? '正在直播'
                            : formatRelative(r.last_living_time) || '从未直播'}
                        </div>
                      </div>
                    </div>

                    <div className={styles.cardTags}>
                      {templateLabels(r).map((name) => (
                        <Tag key={name} color="blue">
                          {name}
                        </Tag>
                      ))}
                      {douyinTemplateLabels(r).map((name) => (
                        <Tag key={name} color="magenta">
                          {name}
                        </Tag>
                      ))}
                      {r.upload_storage_platform && (
                        <Tag color="geekblue">
                          {STORAGE_LABELS[r.upload_storage_platform] ||
                            r.upload_storage_platform}
                        </Tag>
                      )}
                      {r.active_state !== 1 && <Tag>未激活</Tag>}
                    </div>

                    <div className={styles.cardActions}>
                      <Button
                        size="small"
                        type="text"
                        icon={<EditOutlined />}
                        onClick={() => setEditing(r)}
                      >
                        编辑
                      </Button>
                      <Popconfirm
                        title={`确认删除「${r.room_name || r.room_url}」？`}
                        description="删除后将停止录制"
                        onConfirm={() => r.id && handleDelete(r.id)}
                      >
                        <Button
                          size="small"
                          type="text"
                          danger
                          icon={<DeleteOutlined />}
                        >
                          删除
                        </Button>
                      </Popconfirm>
                    </div>
                  </div>
                </div>
              </Col>
            );
          })}
        </Row>
      )}

      <ModalForm
        title="新建直播间"
        open={createOpen}
        onOpenChange={setCreateOpen}
        modalProps={{ destroyOnClose: true }}
        initialValues={{ active_state: true, auto_dance_clip: false, cover_mode: '' }}
        onFinish={async (values) => {
          if (values.cover_mode === 'custom' && !values.custom_cover_path) {
            message.warning('请选择「自定义上传」后上传封面图片，或改用其他封面模式');
            return false;
          }
          await addLiveRoom({
            ...values,
            active_state: values.active_state ? 1 : 0,
            auto_dance_clip: values.auto_dance_clip ? 1 : 0,
          });
          fetchData();
          return true;
        }}
      >
        <RoomFormFields />
      </ModalForm>

      <ModalForm
        key={editing?.id ?? 'closed'}
        title="编辑直播间"
        open={!!editing}
        onOpenChange={(open) => {
          if (!open) setEditing(undefined);
        }}
        modalProps={{ destroyOnClose: true }}
        initialValues={
          editing
            ? {
                ...editing,
                active_state: editing.active_state === 1,
                auto_dance_clip: editing.auto_dance_clip === 1,
                // 多选字段：null 归一为 undefined；兼容只有旧单模板字段的历史数据
                bili_upload_template_ids:
                  editing.bili_upload_template_ids ??
                  (editing.bili_upload_template_id != null
                    ? [editing.bili_upload_template_id]
                    : undefined),
                douyin_upload_template_ids:
                  editing.douyin_upload_template_ids ?? undefined,
                // null 归一为 ''（跟随模板），对齐 Radio 选项值
                cover_mode: editing.cover_mode ?? '',
              }
            : undefined
        }
        onFinish={async (values) => {
          if (!editing?.id) return false;
          if (values.cover_mode === 'custom' && !values.custom_cover_path) {
            message.warning('请选择「自定义上传」后上传封面图片，或改用其他封面模式');
            return false;
          }
          await updateLiveRoom({
            id: editing.id,
            ...values,
            active_state: values.active_state ? 1 : 0,
            auto_dance_clip: values.auto_dance_clip ? 1 : 0,
          });
          fetchData();
          return true;
        }}
      >
        <RoomFormFields />
      </ModalForm>
    </>
  );
};

export default LiveRoomList;
