import services from '@/services/luboman';
import {
  ActionType,
  ModalForm,
  ProForm,
  ProFormDigit,
  ProFormList,
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
  listBiliUploadTemplate,
  addBiliUploadTemplate,
  updateBiliUploadTemplate,
  delBiliUploadTemplate,
} = services.BiliUploadTemplate;
const { listBiliAccount } = services.BiliAccount;
const { getBiliArchivePre } = services.System;

const DEFAULT_TID_OPTIONS = [{ label: '电子竞技', value: 171 }];

const LINES_OPTIONS = ['AUTO', 'bda2', 'kodo', 'ws', 'qn', 'cos'].map((v) => ({
  label: v,
  value: v,
}));

/**
 * 投稿模板表单字段（新建/编辑共用）。
 * 注意：后端 update_bili_upload_template 仅更新白名单字段（template_name, bili_account_id,
 * tags, description, tid, copyright, cover_path, dynamic, dtime, dolby, hires, open_elec,
 * no_reprint, credits, up_selection_reply, up_close_reply, up_close_danmu）；
 * title / threads / lines 仅在新建时写入，编辑时后端会静默忽略。
 */
const TemplateFormFields: React.FC<{
  accountOptions: { label: string; value: number }[];
  tidOptions: { label: string; value: number }[];
}> = ({ accountOptions, tidOptions }) => (
  <>
    <ProForm.Group>
      <ProFormText
        name="template_name"
        label="模板名称"
        width="md"
        rules={[{ required: true, message: '请输入模板名称' }]}
      />
      <ProFormSelect
        name="bili_account_id"
        label="投稿账号"
        width="md"
        options={accountOptions}
        rules={[{ required: true, message: '请选择投稿账号' }]}
      />
    </ProForm.Group>
    <ProForm.Group>
      <ProFormSelect
        name="tid"
        label="投稿分区"
        width="md"
        options={tidOptions}
        placeholder="电子竞技"
        extra="无激活账号时仅显示默认分区"
      />
      <ProFormSelect
        name="copyright"
        label="版权"
        width="md"
        options={[
          { label: '自制', value: 1 },
          { label: '转载', value: 2 },
        ]}
      />
    </ProForm.Group>
    <ProFormText
      name="title"
      label="投稿标题"
      placeholder="{title} 第一视角 %Y-%m-%d {streamer}"
      extra="支持 {title} {streamer} {url} 与 strftime 变量"
    />
    <ProFormSelect
      name="tags"
      label="标签"
      mode="tags"
      placeholder="输入后回车添加（后端会自动补『录播Man』）"
      fieldProps={{ tokenSeparators: [','] }}
    />
    <ProFormTextArea
      name="description"
      label="简介"
      fieldProps={{ autoSize: { minRows: 3, maxRows: 8 } }}
    />
    <ProForm.Group>
      <ProFormText name="cover_path" label="封面路径" width="md" />
      <ProFormText name="dynamic" label="空间动态" width="md" />
    </ProForm.Group>
    <ProForm.Group>
      <ProFormDigit
        name="dtime"
        label="定时发布(时间戳)"
        width="sm"
        extra="距提交大于 2 小时"
      />
      <ProFormDigit
        name="threads"
        label="并发上传数"
        width="sm"
        min={1}
        max={10}
      />
      <ProFormSelect
        name="lines"
        label="上传线路"
        width="sm"
        options={LINES_OPTIONS}
      />
    </ProForm.Group>
    <ProForm.Group label="选项">
      <ProFormSwitch name="dolby" label="杜比音效" />
      <ProFormSwitch name="hires" label="Hi-Res" />
      <ProFormSwitch name="open_elec" label="充电面板" />
      <ProFormSwitch name="no_reprint" label="禁止转载声明" />
      <ProFormSwitch name="up_selection_reply" label="精选评论" />
      <ProFormSwitch name="up_close_reply" label="关闭评论" />
      <ProFormSwitch name="up_close_danmu" label="关闭弹幕" />
    </ProForm.Group>
    <ProFormList
      name="credits"
      label="简介 @"
      creatorButtonProps={{ creatorButtonText: '添加 @"用户"' }}
      min={0}
    >
      <ProForm.Group key="credits-group">
        <ProFormText name="username" label="用户名" />
        <ProFormDigit name="uid" label="UID" />
      </ProForm.Group>
    </ProFormList>
  </>
);

const BiliUploadTemplateList: React.FC = () => {
  const actionRef = useRef<ActionType>();
  const [createOpen, setCreateOpen] = useState(false);
  const [editing, setEditing] = useState<API.BiliUploadTemplateInfo>();

  const [accountOptions, setAccountOptions] = useState<
    { label: string; value: number }[]
  >([]);
  const [tidOptions, setTidOptions] = useState<
    { label: string; value: number }[]
  >(DEFAULT_TID_OPTIONS);
  const [tidFallback, setTidFallback] = useState(false);

  useEffect(() => {
    listBiliAccount()
      .then((list) => {
        setAccountOptions(
          (list || []).map((a) => ({
            label: a.account_name || `账号#${a.id}`,
            value: a.id as number,
          })),
        );
      })
      .catch(() => setAccountOptions([]));

    // 拉取投稿分区；无激活账号时后端返回业务失败，跳过统一错误提示并降级
    getBiliArchivePre({ skipErrorHandler: true })
      .then((pre) => {
        const typeList = pre?.data?.type_list;
        if (typeList && typeList.length) {
          setTidOptions(
            typeList.map((t) => ({ label: t.typename, value: t.tid })),
          );
          setTidFallback(false);
        } else {
          setTidOptions(DEFAULT_TID_OPTIONS);
          setTidFallback(true);
        }
      })
      .catch(() => {
        setTidOptions(DEFAULT_TID_OPTIONS);
        setTidFallback(true);
      });
  }, []);

  const handleDelete = async (id: number) => {
    await delBiliUploadTemplate(id);
    actionRef.current?.reload();
  };

  return (
    <>
    <ProTable<API.BiliUploadTemplateInfo>
      headerTitle="B 站投稿模板"
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
          const data = await listBiliUploadTemplate();
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
          dataIndex: 'bili_account_id',
          width: 140,
          render: (_, r) => {
            if (r.bili_account_id == null) return '-';
            const opt = accountOptions.find(
              (o) => o.value === r.bili_account_id,
            );
            return opt ? opt.label : `账号#${r.bili_account_id}`;
          },
        },
        {
          title: '分区',
          dataIndex: 'tid',
          width: 120,
          render: (_, r) => {
            if (r.tid == null) return '-';
            const opt = tidOptions.find((o) => o.value === r.tid);
            return opt ? opt.label : r.tid;
          },
        },
        { title: '标题', dataIndex: 'title', ellipsis: true },
        {
          title: '版权',
          dataIndex: 'copyright',
          width: 80,
          render: (_, r) => (
            <Badge
              status={r.copyright === 2 ? 'default' : 'success'}
              text={r.copyright === 2 ? '转载' : '自制'}
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

    {tidFallback && (
      <Alert
        style={{ marginTop: 8 }}
        type="info"
        showIcon
        message="未获取到投稿分区列表"
        description="需至少一个启用的 B 站账号，新建/编辑模板时分区仅显示默认『电子竞技』。"
      />
    )}

    <ModalForm
      title="新建投稿模板"
      width={760}
      open={createOpen}
      onOpenChange={setCreateOpen}
      modalProps={{ destroyOnClose: true }}
      onFinish={async (values) => {
        await addBiliUploadTemplate({
          ...values,
          copyright: values.copyright ?? 1,
          tid: values.tid ?? 171,
        });
        actionRef.current?.reload();
        return true;
      }}
    >
      <TemplateFormFields
        accountOptions={accountOptions}
        tidOptions={tidOptions}
      />
    </ModalForm>

    <ModalForm
      key={editing?.id ?? 'closed'}
      title="编辑投稿模板"
      width={760}
      open={!!editing}
      onOpenChange={(open) => {
        if (!open) setEditing(undefined);
      }}
      modalProps={{ destroyOnClose: true }}
      initialValues={editing || undefined}
      onFinish={async (values) => {
        if (!editing?.id) return false;
        await updateBiliUploadTemplate({ id: editing.id, ...values });
        actionRef.current?.reload();
        return true;
      }}
    >
      <TemplateFormFields
        accountOptions={accountOptions}
        tidOptions={tidOptions}
      />
    </ModalForm>
    </>
  );
};

export default BiliUploadTemplateList;
