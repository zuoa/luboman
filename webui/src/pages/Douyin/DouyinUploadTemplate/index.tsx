import services from '@/services/luboman';
import {
  ActionType,
  ModalForm,
  ProForm,
  ProFormDigit,
  ProFormSelect,
  ProFormSwitch,
  ProFormText,
  ProFormTextArea,
  ProTable,
} from '@ant-design/pro-components';
import { ReloadOutlined } from '@ant-design/icons';
import { Alert, Badge, Button, Popconfirm, Space } from 'antd';
import React, { useEffect, useRef, useState } from 'react';

const {
  listDouyinUploadTemplate,
  addDouyinUploadTemplate,
  updateDouyinUploadTemplate,
  delDouyinUploadTemplate,
} = services.DouyinUploadTemplate;
const { listDouyinAccount } = services.DouyinAccount;

/**
 * 抖音投稿模板表单字段（新建/编辑共用）。
 * 与 B 站模板对齐但去掉分区/版权等 B 站字段；vertical_crop 控制切片是否裁竖屏。
 */
const TemplateFormFields: React.FC<{
  accountOptions: { label: string; value: number }[];
}> = ({ accountOptions }) => (
  <>
    <ProForm.Group>
      <ProFormText
        name="template_name"
        label="模板名称"
        width="md"
        rules={[{ required: true, message: '请输入模板名称' }]}
      />
      <ProFormSelect
        name="douyin_account_id"
        label="投稿账号"
        width="md"
        options={accountOptions}
        rules={[{ required: true, message: '请选择投稿账号' }]}
      />
    </ProForm.Group>
    <ProFormText
      name="title"
      label="投稿标题"
      placeholder="【{room_name}】舞蹈片段{seq}"
      extra="支持 {room_name} {room_title} 等占位符与 strftime 变量；抖音标题上限 30 字，超出自动截断"
    />
    <ProFormSelect
      name="tags"
      label="话题"
      mode="tags"
      placeholder="输入后回车添加（不含 # 号）"
      fieldProps={{ tokenSeparators: [','] }}
    />
    <ProFormTextArea
      name="description"
      label="作品描述"
      fieldProps={{ autoSize: { minRows: 3, maxRows: 8 } }}
    />
    <ProForm.Group>
      <ProFormText name="cover_path" label="封面路径" width="md" />
      <ProFormDigit
        name="dtime"
        label="定时发布(时间戳)"
        width="md"
        extra="需距提交 2 小时~7 天内，越界自动改为立即发布"
      />
    </ProForm.Group>
    <ProForm.Group>
      <ProFormSwitch
        name="vertical_crop"
        label="切片转竖屏"
        extra="舞蹈切片裁中栏转 9:16 竖屏（1080x1920），仅对切片生效"
      />
    </ProForm.Group>
  </>
);

const DouyinUploadTemplateList: React.FC = () => {
  const actionRef = useRef<ActionType>();
  const [createOpen, setCreateOpen] = useState(false);
  const [editing, setEditing] = useState<API.DouyinUploadTemplateInfo>();

  const [accountOptions, setAccountOptions] = useState<
    { label: string; value: number }[]
  >([]);

  useEffect(() => {
    listDouyinAccount()
      .then((list) => {
        setAccountOptions(
          (list || []).map((a) => ({
            label: a.account_name || `账号#${a.id}`,
            value: a.id as number,
          })),
        );
      })
      .catch(() => setAccountOptions([]));
  }, []);

  const handleDelete = async (id: number) => {
    await delDouyinUploadTemplate(id);
    actionRef.current?.reload();
  };

  return (
    <>
      <Alert
        style={{ marginBottom: 12 }}
        type="warning"
        showIcon
        message="抖音查重严格"
        description="同一切片投稿到多个抖音账号容易被判搬运/限流，建议一个直播间只绑定一个抖音模板；多号分发请错开标题与发布时间。"
      />
      <ProTable<API.DouyinUploadTemplateInfo>
        headerTitle="抖音投稿模板"
        actionRef={actionRef}
        rowKey="id"
        search={false}
        pagination={false}
        size="middle"
        options={false}
        toolBarRender={() => [
          <Button
            key="reload"
            icon={<ReloadOutlined />}
            onClick={() => actionRef.current?.reload()}
          >
            刷新
          </Button>,
          <Button
            key="create"
            type="primary"
            onClick={() => setCreateOpen(true)}
          >
            新建
          </Button>,
        ]}
        request={async () => {
          try {
            const data = await listDouyinUploadTemplate();
            return { data: data || [], success: true };
          } catch {
            return { data: [], success: false };
          }
        }}
        columns={[
          { title: 'ID', dataIndex: 'id', width: 60 },
          { title: '模板名称', dataIndex: 'template_name', ellipsis: true },
          {
            title: '投稿账号',
            dataIndex: 'douyin_account_id',
            width: 140,
            render: (_, r) => {
              if (r.douyin_account_id == null) return '-';
              const opt = accountOptions.find(
                (o) => o.value === r.douyin_account_id,
              );
              return opt ? opt.label : `账号#${r.douyin_account_id}`;
            },
          },
          { title: '标题', dataIndex: 'title', ellipsis: true },
          {
            title: '切片转竖屏',
            dataIndex: 'vertical_crop',
            width: 110,
            render: (_, r) => (
              <Badge
                status={r.vertical_crop === 0 ? 'default' : 'success'}
                text={r.vertical_crop === 0 ? '关闭' : '开启'}
              />
            ),
          },
          {
            title: '操作',
            dataIndex: 'option',
            valueType: 'option',
            width: 110,
            render: (_, record) => (
              <Space>
                <a onClick={() => setEditing(record)}>编辑</a>
                <Popconfirm
                  title="确认删除该模板？"
                  onConfirm={() => record.id && handleDelete(record.id)}
                >
                  <a>删除</a>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />

      <ModalForm
        title="新建抖音投稿模板"
        width={760}
        open={createOpen}
        onOpenChange={setCreateOpen}
        modalProps={{ destroyOnClose: true }}
        onFinish={async (values) => {
          await addDouyinUploadTemplate({
            ...values,
            vertical_crop: values.vertical_crop === false ? 0 : 1,
          } as API.DouyinUploadTemplateInfo);
          actionRef.current?.reload();
          return true;
        }}
      >
        <TemplateFormFields accountOptions={accountOptions} />
      </ModalForm>

      <ModalForm
        key={editing?.id ?? 'closed'}
        title="编辑抖音投稿模板"
        width={760}
        open={!!editing}
        onOpenChange={(open) => {
          if (!open) setEditing(undefined);
        }}
        modalProps={{ destroyOnClose: true }}
        initialValues={
          editing
            ? { ...editing, vertical_crop: editing.vertical_crop !== 0 }
            : undefined
        }
        onFinish={async (values) => {
          if (!editing?.id) return false;
          await updateDouyinUploadTemplate({
            id: editing.id,
            ...values,
            vertical_crop: values.vertical_crop === false ? 0 : 1,
          });
          actionRef.current?.reload();
          return true;
        }}
      >
        <TemplateFormFields accountOptions={accountOptions} />
      </ModalForm>
    </>
  );
};

export default DouyinUploadTemplateList;
