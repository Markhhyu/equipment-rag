import 'element-plus/dist/index.css'
import '../styles/base.css'
import './workflow.css'
import WorkflowApp from './WorkflowApp.vue'
import { mountProtectedPage } from '../shared/bootstrap'

mountProtectedPage(WorkflowApp, 'workflow')
