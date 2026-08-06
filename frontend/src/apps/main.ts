import 'element-plus/dist/index.css'
import '../styles/base.css'
import './apps.css'
import AppsApp from './AppsApp.vue'
import { mountProtectedPage } from '../shared/bootstrap'

mountProtectedPage(AppsApp)
